"""Automatic updates from GitHub Releases.

Every few hours ArtSafe asks GitHub for the latest release. If it's newer, it
downloads ArtSafe-<version>.zip and its .sig file and checks the signature
against the public key below. Only someone with the matching private key
(kept on the developer's PC, never uploaded) can make a valid update.
Then a small script swaps the app folder while ArtSafe is closed and starts
the new version. If the swap fails, the old version is put back.

Her backups and settings live in AppData, not in the app folder, so updating
never touches them.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import __version__
from .logs import logs_dir

import logging
log = logging.getLogger(__name__)

UPDATE_REPO = os.environ.get("ARTSAFE_UPDATE_REPO_TEST") or "DewitteRuben/artsafe"
PUBLIC_KEY_HEX = "a7dd91d3f06c9b33ca8a09d7c33799ff3a65acdfb4399725bb7ee01b4159901e"
CHECK_EVERY_SECONDS = 6 * 3600
# Test hooks: point at a local server / check sooner. Updates must still carry a valid signature.
API_BASE = os.environ.get("ARTSAFE_UPDATE_API", "https://api.github.com")
FIRST_CHECK_DELAY = int(os.environ.get("ARTSAFE_UPDATE_DELAY", "120"))
USER_AGENT = f"ArtSafe/{__version__}"


def parse_version(v: str) -> tuple[int, ...]:
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def updates_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ArtSafe" / "updates"


def install_dir() -> Path | None:
    """The folder ArtSafe.exe runs from, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def can_update_in_place(folder: Path) -> bool:
    """We need to be able to rename the app folder, so its parent must be writable."""
    try:
        with tempfile.NamedTemporaryFile(dir=folder.parent, prefix=".artsafe-write-test"):
            pass
        return True
    except OSError:
        return False


def verify(data_path: Path, signature: bytes, public_key_hex: str = PUBLIC_KEY_HEX) -> bool:
    """Signature is over the SHA-256 digest of the zip."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    digest = hashlib.sha256()
    with open(data_path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            digest.update(chunk)
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    try:
        key.verify(signature, digest.digest())
        return True
    except InvalidSignature:
        return False


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(repo: str = "") -> dict | None:
    repo = repo or UPDATE_REPO
    if not repo:
        return None
    with _get(f"{API_BASE}/repos/{repo}/releases/latest") as r:
        data = json.load(r)
    version = data.get("tag_name", "").lstrip("vV")
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    zip_name, sig_name = f"ArtSafe-{version}.zip", f"ArtSafe-{version}.zip.sig"
    if zip_name not in assets or sig_name not in assets:
        return None
    return {"version": version, "zip": assets[zip_name], "sig": assets[sig_name],
            "notes": data.get("body") or ""}


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _get(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    os.replace(tmp, dest)


def prepare(release: dict) -> Path:
    """Download, verify and unpack an update. Returns the new app folder."""
    d = updates_dir()
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    zpath = d / f"ArtSafe-{release['version']}.zip"
    spath = d / (zpath.name + ".sig")
    download(release["zip"], zpath)
    download(release["sig"], spath)
    if not verify(zpath, spath.read_bytes()):
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError("update signature is not valid; ignoring it")
    out = d / "new"
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():  # refuse anything that would land outside the folder
            target = (out / name).resolve()
            if out.resolve() not in target.parents and target != out.resolve():
                raise ValueError("unsafe path in update")
        z.extractall(out)
    app = out / "ArtSafe"
    if not (app / "ArtSafe.exe").is_file():
        raise ValueError("update doesn't contain ArtSafe.exe")
    return app


APPLY_PS1 = r"""
param([int]$WaitPid, [string]$NewDir, [string]$AppDir, [string]$Version, [string]$LogDir, [switch]$Reopen)
$ErrorActionPreference = "Stop"
$log = Join-Path $LogDir "update.log"
# Reopen the window afterwards if it was open, otherwise start quietly in the tray.
$mode = if ($Reopen) { "--show" } else { "--tray" }
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format s), $m) }
try { Wait-Process -Id $WaitPid -Timeout 60 } catch {}
Start-Sleep -Seconds 1
$old = "$AppDir.old"
try {
  if (Test-Path $old) { Remove-Item $old -Recurse -Force }
  Move-Item $AppDir $old
  try {
    Move-Item $NewDir $AppDir
    Log "updated to $Version"
    Start-Process (Join-Path $AppDir "ArtSafe.exe") -ArgumentList $mode, "--updated" -WorkingDirectory $AppDir
    Start-Sleep -Seconds 5
    Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
  } catch {
    Log "could not move new version in: $_ ; restoring old version"
    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item $old $AppDir
    Start-Process (Join-Path $AppDir "ArtSafe.exe") -ArgumentList $mode
  }
} catch {
  Log "could not replace app folder (in use?): $_ ; keeping current version"
  Start-Process (Join-Path $AppDir "ArtSafe.exe") -ArgumentList $mode
}
"""


def launch_apply(new_app: Path, version: str, reopen: bool = False) -> None:
    """Start the swap script; the caller must quit ArtSafe right after."""
    app_dir = install_dir()
    if app_dir is None:
        raise RuntimeError("not running as an installed app")
    script = updates_dir() / "apply.ps1"
    script.write_text(APPLY_PS1, encoding="utf-8")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-File", str(script), "-WaitPid", str(os.getpid()), "-NewDir", str(new_app),
           "-AppDir", str(app_dir), "-Version", version, "-LogDir", str(logs_dir())]
    if reopen:
        cmd.append("-Reopen")
    # NO_WINDOW | NEW_PROCESS_GROUP, and break away from any job so it outlives ArtSafe.
    # (Not DETACHED_PROCESS: PowerShell won't run without a console.)
    base = 0x08000000 | 0x00000200
    for flags in (base | 0x01000000, base):
        try:
            p = subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                                 cwd=str(updates_dir()))  # never inside the app folder
            log.info("Update script started (pid %s) to install %s", p.pid, version)
            return
        except OSError:
            continue
    raise RuntimeError("couldn't start the update script")


class Updater:
    """Checks in the background; calls on_ready(version, folder) when an update is ready to install."""

    def __init__(self, enabled: Callable[[], bool], on_ready: Callable[[str, Path], None],
                 on_status: Callable[[str], None] = lambda s: None):
        self.enabled = enabled
        self.on_ready = on_ready
        self.on_status = on_status
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._manual = False
        self.ready: tuple[str, Path] | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="artsafe-updater", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def check_now(self) -> None:
        self._manual = True
        self._wake.set()

    def _run(self) -> None:
        self._wake.wait(FIRST_CHECK_DELAY)
        while not self._stop.is_set():
            manual, self._manual = self._manual, False
            if (self.enabled() or manual) and self.ready is None:
                self.on_status(self.check_once(manual))
            self._wake.clear()
            self._wake.wait(CHECK_EVERY_SECONDS)

    def check_once(self, manual: bool = False) -> str:
        if not UPDATE_REPO:
            return "Updates aren't set up for this copy of ArtSafe"
        app_dir = install_dir()
        if app_dir is None:
            return "Running from source; updates only work in the installed app"
        try:
            rel = latest_release()
        except Exception as e:
            log.warning("Update check failed: %r", e)
            return f"Couldn't check for updates ({type(e).__name__}); will try again later"
        if not rel or not is_newer(rel["version"]):
            return f"ArtSafe is up to date (version {__version__})"
        if not can_update_in_place(app_dir):
            return (f"Version {rel['version']} is available, but ArtSafe's folder can't be changed. "
                    "Move the ArtSafe folder to Documents to get automatic updates.")
        try:
            folder = prepare(rel)
        except Exception as e:
            log.error("Update %s download/verify failed: %s", rel["version"], e, exc_info=True)
            return f"Couldn't download the update ({e}); will try again later"
        log.info("Update %s downloaded and verified", rel["version"])
        self.ready = (rel["version"], folder)
        self.on_ready(rel["version"], folder)
        return f"Version {rel['version']} downloaded and will be installed shortly"
