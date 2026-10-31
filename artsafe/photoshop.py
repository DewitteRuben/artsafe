"""Snapshots of open Photoshop documents, taken by asking Photoshop to "save a copy".

Photoshop is controlled the same way its own scripts are (COM + ExtendScript).
The real file is never touched: Photoshop writes a .psb copy into ArtSafe's
staging folder, which is then stored as an "auto-save" version.

To avoid freezing Photoshop mid-stroke, an automatic snapshot waits until
the mouse and keyboard have been still for a few seconds.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .vault import Vault, VersionRow

import logging
log = logging.getLogger(__name__)

APP_LABEL = "Photoshop snapshot"
IDLE_SECONDS = 8          # wait for a pause this long before an automatic snapshot
MAX_WAIT_SECONDS = 5 * 60  # ...but don't wait longer than this past the due time

LIST_JS = r"""
(function () {
  var out = [];
  var activeName = app.documents.length ? app.activeDocument.name : "";
  for (var i = 0; i < app.documents.length; i++) {
    var d = app.documents[i], full = "";
    try { full = d.fullName.fsName; } catch (e) {}
    out.push([d.name, full, d.saved ? 1 : 0, d.name == activeName ? 1 : 0].join("\t"));
  }
  return out.join("\n");
})();
"""

SAVE_JS = r"""
(function (name, target) {
  var doc = app.documents.getByName(name), prev = app.activeDocument;
  var dialogs = app.displayDialogs, switched = false, result;
  var before = ""; try { before = doc.fullName.fsName; } catch (e) {}
  app.displayDialogs = DialogModes.NO;
  try {
    if (doc != prev) { app.activeDocument = doc; switched = true; }
    try {
      var opts = new PhotoshopSaveOptions();
      opts.layers = true; opts.maximizeCompatibility = true; opts.embedColorProfile = true;
      doc.saveAs(new File(target + ".psd"), opts, true, Extension.LOWERCASE);  // true = save a copy
      result = target + ".psd";
    } catch (tooBig) {
      // documents over 2 GB / 30,000 px can only be saved as .psb
      var desc = new ActionDescriptor();
      desc.putObject(charIDToTypeID("As  "), stringIDToTypeID("largeDocumentFormat"), new ActionDescriptor());
      desc.putPath(charIDToTypeID("In  "), new File(target + ".psb"));
      desc.putBoolean(charIDToTypeID("Cpy "), true);
      executeAction(charIDToTypeID("save"), desc, DialogModes.NO);
      result = target + ".psb";
    }
  } catch (e) {
    result = "error: " + e;
  }
  // Safety check: the open document must still belong to her own file, not our copy.
  var after = ""; try { after = doc.fullName.fsName; } catch (e) {}
  if (after != before) result = "moved: " + after;
  try { if (switched) app.activeDocument = prev; } catch (e) {}
  app.displayDialogs = dialogs;
  return result;
})(%s, %s);
"""


@dataclass
class OpenDoc:
    name: str
    full_path: str
    saved: bool
    active: bool


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def seconds_since_input() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def last_input_tick() -> int:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


def photoshop_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Photoshop.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return False
    return "photoshop.exe" in out.lower()


def staging_name(doc: OpenDoc) -> str:
    """A stable file name per document, so its snapshots are grouped together."""
    base = re.sub(r'[<>:"/\\|?*]', "_", Path(doc.name).stem) or "Untitled"
    tag = hashlib.sha1((doc.full_path or doc.name).lower().encode()).hexdigest()[:6]
    return f"{base} [{tag}]"  # Photoshop adds .psd or .psb


def display_name(path: str) -> str:
    """'dragon [a1b2c3].psb' -> 'dragon'"""
    return re.sub(r" \[[0-9a-f]{6}\]$", "", Path(path).stem)


class Snapshotter:
    def __init__(self, cfg: Config, vault: Vault,
                 on_backup: Callable[[VersionRow], None] = lambda v: None,
                 on_status: Callable[[str], None] = lambda s: None):
        self.cfg = cfg
        self.vault = vault
        self.on_backup = on_backup
        self.on_status = on_status
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._manual = False
        self._manual_done: Callable[[str], None] | None = None
        self._last_input_at_snapshot: int | None = None
        self._thread: threading.Thread | None = None
        self.last_result = ""

    @property
    def staging(self) -> Path:
        return Path(self.vault.root) / "staging"  # follows the backups if they're moved

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="artsafe-photoshop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def snapshot_now(self, done: Callable[[str], None] | None = None) -> None:
        """Snapshot every open document right away (from the tray / button)."""
        self._manual = True
        self._manual_done = done
        self._wake.set()

    # ---------- worker ----------
    def _run(self) -> None:
        import pythoncom

        pythoncom.CoInitialize()
        next_due = time.time() + self._interval()
        try:
            while not self._stop.is_set():
                self._wake.wait(5)
                self._wake.clear()
                if self._stop.is_set():
                    break
                if self._manual:
                    self._manual = False
                    msg = self._take(all_docs=True, force=True)
                    if self._manual_done:
                        self._manual_done(msg)
                    next_due = time.time() + self._interval()
                    continue
                if not self.cfg.ps_snapshots or self.cfg.paused:
                    next_due = time.time() + self._interval()
                    continue
                now = time.time()
                if now < next_due:
                    continue
                # due: wait for a pause in her work, unless we've waited too long already
                if seconds_since_input() < IDLE_SECONDS and now < next_due + MAX_WAIT_SECONDS:
                    continue
                self._take(all_docs=False, force=False)
                next_due = time.time() + self._interval()
        finally:
            pythoncom.CoUninitialize()

    def _interval(self) -> float:
        return max(1, int(self.cfg.ps_snapshot_minutes)) * 60

    def _connect(self):
        import win32com.client

        if not photoshop_running():
            return None
        # Photoshop is running, so this attaches to it instead of starting a new one.
        return win32com.client.Dispatch("Photoshop.Application")

    def _take(self, all_docs: bool, force: bool) -> str:
        try:
            ps = self._connect()
        except Exception as e:
            return self._result(f"Couldn't connect to Photoshop ({e})")
        if ps is None:
            return self._result("Photoshop isn't open")

        # Automatic snapshots skip when she hasn't touched mouse or keyboard since the last one.
        tick = last_input_tick()
        if not force and tick == self._last_input_at_snapshot:
            return self._result("Nothing new to snapshot")

        try:
            docs = self._list(ps)
        except Exception as e:
            return self._result(f"Photoshop is busy, will try again ({_short(e)})")
        if not docs:
            return self._result("No documents open in Photoshop")

        targets = [d for d in docs if all_docs or d.active]
        if not force:
            # a document with no unsaved changes is already protected by its own saved file
            targets = [d for d in targets if not d.saved]
        if not targets:
            return self._result("No unsaved changes to snapshot")

        self.staging.mkdir(parents=True, exist_ok=True)
        taken, failed = [], []
        for d in targets:
            base = self.staging / staging_name(d)
            try:
                res = str(run_js(ps, SAVE_JS % (json.dumps(d.name), json.dumps(str(base).replace("\\", "/")))))
            except Exception as e:
                failed.append(f"{d.name} ({_short(e)})")
                continue
            if res.startswith("moved: "):
                # Photoshop didn't keep it as a copy. Stop snapshots and leave every file where it is.
                self.cfg.ps_snapshots = False
                self.cfg.save()
                return self._result(
                    f"⚠️ Photoshop now thinks “{d.name}” lives in ArtSafe's folder. Automatic snapshots "
                    f"were turned off. In Photoshop, use File → Save As to save it back where it belongs.",
                    announce=True)
            stage = Path(res)
            try:
                in_staging = stage.parent.resolve() == self.staging.resolve()
            except OSError:
                in_staging = False
            if not in_staging or not stage.is_file():
                failed.append(f"{d.name} ({_short(Exception(res))})")
                continue
            try:
                v = self.vault.backup_file(stage, reason="snapshot", kind="autosave", app=APP_LABEL)
                if v:
                    self.on_backup(v)
                taken.append(d.name)
            finally:
                stage.unlink(missing_ok=True)
        self._last_input_at_snapshot = tick
        if taken:
            msg = f"Snapshot of {', '.join(taken)} at {time.strftime('%H:%M')}"
        else:
            msg = "Snapshot failed"
        if failed:
            msg += " · couldn't snapshot " + ", ".join(failed)
        return self._result(msg, announce=True)

    def _list(self, ps) -> list[OpenDoc]:
        raw = run_js(ps, LIST_JS) or ""
        docs = []
        for line in str(raw).splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                docs.append(OpenDoc(parts[0], parts[1], parts[2] == "1", parts[3] == "1"))
        return docs

    def _result(self, msg: str, announce: bool = False) -> str:
        """Routine "nothing to do" results aren't shown in the window, to keep it calm."""
        self.last_result = msg
        if "⚠️" in msg:
            from . import logs
            logs.event("Photoshop snapshot safety stop", msg)
        elif "couldn't" in msg.lower() or "failed" in msg.lower():
            log.error("Photoshop snapshot problem: %s", msg)
        else:
            log.info("Snapshot: %s", msg)
        if announce:
            self.on_status(msg)
        return msg


BAD_ARGS = (-2147352571, -2147352562)  # COM "type mismatch" / "bad parameter count"


def run_js(ps, js: str):
    """Run ExtendScript in Photoshop without ever showing its debugger."""
    try:
        return ps.DoJavaScript(js, None, 1)
    except Exception as e:
        if getattr(e, "hresult", None) in BAD_ARGS or isinstance(e, TypeError):
            return ps.DoJavaScript(js)
        raise


def _short(e: Exception) -> str:
    s = str(e)
    return s if len(s) < 80 else s[:77] + "…"
