"""Logging, plus error reports sent to the developer's Discord channel.

Everything goes to %LOCALAPPDATA%\\ArtSafe\\logs\\artsafe.log (kept small, rotated).
Errors, crashes and a few notable events (update installed, disk full, ...)
are also posted to a Discord webhook, so problems on her PC can be fixed
without access to it. Reports are rate-limited and her user name is replaced
by "~" in paths.

The webhook URL is not in the source code (the repo is public): build.ps1
writes it into artsafe/_secrets.py from %USERPROFILE%\\.artsafe\\webhook.txt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import platform
import queue
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

from . import __version__

try:
    from ._secrets import DISCORD_WEBHOOK
except ImportError:
    DISCORD_WEBHOOK = ""

log = logging.getLogger("artsafe")

MAX_REPORTS_PER_DAY = 25
SAME_ERROR_EVERY_SECONDS = 6 * 3600
_reporter: "DiscordReporter | None" = None


def logs_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ArtSafe" / "logs"


def log_file() -> Path:
    return logs_dir() / "artsafe.log"


def scrub(text: str) -> str:
    """Hide her user name in paths."""
    home = str(Path.home())
    return text.replace(home, "~").replace(home.replace("\\", "/"), "~")


def machine_tag() -> str:
    """Short, anonymous id so reports from different PCs can be told apart."""
    raw = f"{platform.node()}-{uuid.getnode()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:6]


class DiscordReporter(logging.Handler):
    """Sends ERROR+ log records (and explicit events) to Discord in the background."""

    def __init__(self, webhook: str, enabled=lambda: True):
        super().__init__(level=logging.ERROR)
        self.webhook = webhook
        self.enabled = enabled
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.sent_today: list[float] = []
        self.last_seen: dict[str, float] = {}
        threading.Thread(target=self._run, name="artsafe-reporter", daemon=True).start()

    # --- logging.Handler ---
    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            # group by where it happened + first line, so the same error isn't sent over and over
            key = f"{record.pathname}:{record.lineno}:{text.splitlines()[0][:80]}"
            self.post("🔴 " + record.levelname.title(), text, key)
        except Exception:
            pass

    def post(self, title: str, text: str, key: str | None = None, attach: Path | None = None) -> bool:
        if not self.webhook or not self.enabled():
            return False
        now = time.time()
        key = key or title + text[:80]
        if attach is None:  # manual reports always go through
            if now - self.last_seen.get(key, 0) < SAME_ERROR_EVERY_SECONDS:
                return False
            self.sent_today = [t for t in self.sent_today if now - t < 86400]
            if len(self.sent_today) >= MAX_REPORTS_PER_DAY:
                return False
            self.sent_today.append(now)
        self.last_seen[key] = now
        try:
            self.q.put_nowait((title, scrub(text), attach))
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while True:
            title, text, attach = self.q.get()
            for attempt in range(3):
                try:
                    self._send(title, text, attach)
                    break
                except Exception:
                    time.sleep(30 * (attempt + 1))  # offline? try again a bit later

    def _send(self, title: str, text: str, attach: Path | None) -> None:
        header = f"**{title}** · ArtSafe {__version__} · PC `{machine_tag()}` · {platform.platform(terse=True)}"
        body = text if len(text) < 1800 else text[:600] + "\n…\n" + text[-1150:]
        content = f"{header}\n```\n{body}\n```"
        payload = json.dumps({"content": content, "username": "ArtSafe"}).encode()
        if attach and attach.exists():
            boundary = uuid.uuid4().hex
            data = attach.read_bytes()[-7_000_000:]  # stay under Discord's upload limit
            parts = [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                f"Content-Type: application/json\r\n\r\n".encode() + payload + b"\r\n",
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
                f"filename=\"{attach.name}\"\r\nContent-Type: text/plain\r\n\r\n".encode()
                + scrub(data.decode("utf-8", "replace")).encode() + b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
            req = urllib.request.Request(self.webhook, data=b"".join(parts), method="POST", headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "ArtSafe"})
        else:
            req = urllib.request.Request(self.webhook, data=payload, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "ArtSafe"})
        urllib.request.urlopen(req, timeout=20).close()


def setup(report_enabled=lambda: True) -> None:
    """Call once at start-up."""
    global _reporter
    logs_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(fh)
    for noisy in ("PIL", "psd_tools", "watchdog", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if DISCORD_WEBHOOK:
        _reporter = DiscordReporter(DISCORD_WEBHOOK, report_enabled)
        root.addHandler(_reporter)

    def excepthook(t, v, tb):
        log.critical("Crash (unhandled error)", exc_info=(t, v, tb))

    def thread_excepthook(args):
        log.critical("Crash in background task %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    log.info("ArtSafe %s starting · %s · Python %s", __version__, platform.platform(), platform.python_version())


def event(title: str, text: str = "") -> None:
    """A notable non-error event worth knowing about remotely (update installed, disk full...)."""
    log.warning("%s %s", title, text)
    if _reporter:
        _reporter.post("🟡 " + title, text or title)


def send_report(note: str = "") -> bool:
    """Manual 'Send report' from Settings: posts a note plus the log file."""
    if not _reporter:
        return False
    for h in logging.getLogger().handlers:
        h.flush()
    return _reporter.post("📨 Report sent from her PC", note or "(no note)", attach=log_file())


def reporting_available() -> bool:
    return bool(DISCORD_WEBHOOK)
