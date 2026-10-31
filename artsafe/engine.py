"""Watches folders and backs files up shortly after they change.

Events are collected and only handled once a file has been quiet for a few
seconds, so a save in progress is never copied half-written. A full rescan runs
at start-up and every 15 minutes in case the watcher missed anything.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import recovery
from .config import Config
from .vault import DiskFull, FileBusy, FileRow, Vault, VersionRow

import logging
log = logging.getLogger(__name__)

SETTLE_SECONDS = 3.0
RESCAN_SECONDS = 15 * 60
PRUNE_SECONDS = 6 * 3600
SIZE_CHECK_SECONDS = 10 * 60
MAX_RETRY_SECONDS = 600
BUSY_RETRY_SECONDS = 20


class Engine:
    def __init__(
        self,
        cfg: Config,
        vault: Vault,
        on_backup: Callable[[VersionRow], None] = lambda v: None,
        on_deleted: Callable[[FileRow], None] = lambda f: None,
        on_status: Callable[[str], None] = lambda s: None,
    ):
        self.cfg = cfg
        self.vault = vault
        self.on_backup = on_backup
        self.on_deleted = on_deleted
        self.on_status = on_status
        self._pending: dict[str, tuple[float, float]] = {}  # path -> (last event, first event)
        self._plock = threading.Lock()
        self._stop = threading.Event()
        self._rescan_now = threading.Event()
        self._observer: Observer | None = None
        self._thread: threading.Thread | None = None
        self.sources: list[recovery.Source] = []
        self._moves: dict[str, str] = {}  # renamed-from -> renamed-to, resolved once things settle
        self._watched_roots: list[str] = []
        self.disk_full = False
        self._last_full_prune = 0.0
        self.last_activity = "Starting…"

    # ---------- lifecycle ----------
    def start(self) -> None:
        self._start_observer()
        self._thread = threading.Thread(target=self._run, name="artsafe-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._rescan_now.set()
        self._stop_observer()
        if self._thread:
            self._thread.join(timeout=5)

    def reload(self) -> None:
        """Call after settings change."""
        self._stop_observer()
        self._start_observer()
        self.request_rescan()

    def request_rescan(self) -> None:
        self._rescan_now.set()

    def _find_sources(self) -> list[recovery.Source]:
        return recovery.find_sources() if self.cfg.capture_autosaves else []

    def classify(self, path: str) -> tuple[str, str | None] | None:
        """("file", None) for her own files, ("autosave", app) for recovery copies, else None."""
        # Checked first: if she protects her whole user folder, Photoshop's recovery files
        # are inside it too, and they must not show up as "deleted" every time she closes a file.
        src = recovery.match(self.sources, path)
        if src:
            return ("autosave", src.app)
        if self.cfg.wants(path):
            return ("file", None)
        return None

    def _start_observer(self) -> None:
        if self.cfg.paused:
            return
        self.sources = self._find_sources()
        obs = Observer()
        handler = _Handler(self)
        roots = list(self.cfg.watched_folders) + [s.root for s in self.sources]
        self._watched_roots = self._available(roots)
        for folder in self._watched_roots:
            if os.path.isdir(folder):
                try:
                    obs.schedule(handler, folder, recursive=True)
                except OSError:
                    log.exception("Can't watch folder %s", folder)
        obs.daemon = True
        obs.start()
        self._observer = obs

    @staticmethod
    def _available(roots: list[str]) -> list[str]:
        return [r for r in dict.fromkeys(roots) if os.path.isdir(r)]

    def _stop_observer(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    # ---------- events ----------
    def queue(self, path: str, delay: float = 0.0) -> None:
        if self.cfg.paused:
            return
        now = time.time()
        with self._plock:
            first = self._pending.get(path, (now, now))[1]
            self._pending[path] = (now + delay, first)

    def handle_move(self, src: str, dest: str) -> None:
        # Decided later: apps often save by renaming the old file away and writing a new one,
        # in which case the original name is back a moment later and nothing was renamed.
        with self._plock:
            self._moves[src] = dest
        self.queue(src)
        self.queue(dest)

    def handle_dir_gone(self, folder: str) -> None:
        for p in self.vault.known_paths_under(folder):
            self.queue(p)

    # ---------- worker ----------
    def _run(self) -> None:
        last_scan = 0.0
        last_prune = 0.0
        last_size_check = time.time()
        while not self._stop.is_set():
            if self._rescan_now.is_set() or time.time() - last_scan > RESCAN_SECONDS:
                self._rescan_now.clear()
                if not self.cfg.paused:
                    self.full_scan()
                last_scan = time.time()
            if time.time() - last_prune > PRUNE_SECONDS:
                try:
                    removed = self.vault.prune()
                    copied = self.vault.sync_cloud_all()
                    log.info("Cleanup removed %d old versions; cloud catch-up copied %d files", removed, copied)
                except Exception:
                    log.exception("Cleanup / cloud catch-up failed")
                last_prune = time.time()
            if time.time() - last_size_check > SIZE_CHECK_SECONDS:
                last_size_check = time.time()
                cap = self.cfg.max_vault_gb
                try:
                    if cap and self.vault.stats()["bytes"] > cap * 1024**3:
                        log.info("Backups are over the %s GB limit; removing oldest versions", cap)
                        self.vault.prune()
                except Exception:
                    log.exception("Size-limit cleanup failed")
            self.process_pending()
            self._rescan_now.wait(1.0)

    def process_pending(self, settle: float = SETTLE_SECONDS) -> None:
        now = time.time()
        with self._plock:
            ready = [p for p, (t, _) in self._pending.items() if now - t >= settle]
            # renamed-from paths first, so history moves before the new name is backed up
            ready.sort(key=lambda p: p not in self._moves)
            firsts = {p: self._pending.pop(p)[1] for p in ready}
        for path in ready:
            self._handle(path, firsts[path])

    def _handle(self, path: str, first_seen: float) -> None:
        what = self.classify(path)
        if os.path.exists(path):
            if os.path.isdir(path) or not what:
                return
            kind, app = what
            with self._plock:
                self._moves.pop(path, None)
            try:
                v = self.vault.backup_file(path, kind=kind, app=app)
                self.disk_full = False
            except FileBusy:
                if time.time() - first_seen < MAX_RETRY_SECONDS:
                    with self._plock:
                        self._pending[path] = (time.time() + BUSY_RETRY_SECONDS, first_seen)
                return
            except DiskFull:
                self._handle_disk_full(path)
                return
            except Exception as e:  # never let one bad file stop the engine
                log.exception("Could not back up %s", path)
                self._status(f"Could not back up {Path(path).name}: {e}")
                return
            if v:
                log.info("Backed up %s (%s, %d bytes)", path, kind, v.size)
                if kind == "autosave":
                    self._status(f"Kept a {app} auto-save at {time.strftime('%H:%M')}")
                else:
                    self._status(f"Backed up {Path(path).name} at {time.strftime('%H:%M')}")
                self.on_backup(v)
        else:
            with self._plock:
                dest = self._moves.pop(path, None)
            if dest and os.path.isfile(dest) and self.classify(dest) == ("file", None) \
                    and self.vault.rename(path, dest):
                log.info("Renamed %s -> %s", path, dest)
                self._status(f"Noticed {Path(path).name} was renamed to {Path(dest).name}")
                return
            f = self.vault.mark_deleted(path)
            # apps delete their own recovery copies when a document closes; that's not her deleting anything
            if f:
                log.info("Gone: %s", path)
            if f and (not what or what[0] == "file"):
                self._status(f"Kept a copy of deleted file {f.name}")
                self.on_deleted(f)

    def _handle_disk_full(self, path: str) -> None:
        if not self.disk_full:
            from . import logs
            logs.event("Disk almost full", "Backups paused until there's more free space.")
        self.disk_full = True
        self._status("⚠️ Disk almost full: backups paused until there's more free space")
        if time.time() - self._last_full_prune > 600:
            self._last_full_prune = time.time()
            try:
                self.vault.prune()
            except Exception:
                log.exception("Cleanup while disk full failed")
        self.queue(path, delay=300)

    def full_scan(self) -> None:
        self._status("Checking your folders…")
        sources = self._find_sources()
        roots = self._available(list(self.cfg.watched_folders) + [s.root for s in sources])
        # restart watching if Photoshop got installed, or a drive was plugged back in
        if sources != self.sources or roots != self._watched_roots:
            self.sources = sources
            if self._observer:
                self._stop_observer()
                self._start_observer()
        for src in self.sources:
            for root, _dirs, files in os.walk(src.root):
                for name in files:
                    p = os.path.join(root, name)
                    if src.matches(p):
                        try:
                            v = self.vault.backup_file(p, kind="autosave", app=src.app)
                            if v:
                                self.on_backup(v)
                        except FileBusy:
                            self.queue(p)
                        except DiskFull:
                            self._handle_disk_full(p)
                            return
                        except Exception:
                            log.exception("Could not back up app backup %s", p)
            for p in self.vault.known_paths_under(src.root):
                self.vault.mark_deleted(p)
        seen = 0
        for folder in list(self.cfg.watched_folders):
            if not os.path.isdir(folder):
                continue
            for root, dirs, files in os.walk(folder):
                if self._stop.is_set():
                    return
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    p = os.path.join(root, name)
                    if not self.cfg.wants(p):
                        continue
                    seen += 1
                    try:
                        v = self.vault.backup_file(p, reason="saved")
                        if v:
                            self.on_backup(v)
                    except FileBusy:
                        self.queue(p)
                    except DiskFull:
                        self._handle_disk_full(p)
                        return
                    except Exception:
                        log.exception("Could not back up %s", p)
            if not os.path.isdir(folder):
                continue  # folder vanished mid-scan (drive unplugged?): don't call its files deleted
            for p in self.vault.known_paths_under(folder):
                if not os.path.exists(p):
                    f = self.vault.mark_deleted(p)
                    if f:
                        self.on_deleted(f)
        n = len(self.cfg.watched_folders)
        if self.disk_full:
            return
        self._status(
            f"Protecting {seen} file{'s' if seen != 1 else ''} in {n} folder{'s' if n != 1 else ''}"
        )

    def _status(self, msg: str) -> None:
        self.last_activity = msg
        self.on_status(msg)


class _Handler(FileSystemEventHandler):
    def __init__(self, engine: Engine):
        self.e = engine

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.e.queue(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.e.queue(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        # Watchdog can't always tell if a deleted path was a folder, so check both.
        self.e.queue(event.src_path)
        self.e.handle_dir_gone(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            for p in self.e.vault.known_paths_under(event.src_path):
                rel = os.path.relpath(p, event.src_path)
                self.e.handle_move(p, os.path.join(event.dest_path, rel))
        else:
            self.e.handle_move(event.src_path, event.dest_path)
