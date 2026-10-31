"""Settings stored as JSON in %APPDATA%\\ArtSafe\\config.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "ArtSafe"

# Photoshop, Photoshop large document, Clip Studio Paint, Illustrator. Always backed up.
REQUIRED_EXTENSIONS = [".psd", ".psb", ".clip", ".ai"]

# Working files of drawing apps: these are what gets backed up by default.
DRAWING_EXTENSIONS = REQUIRED_EXTENSIONS + [
    ".kra", ".procreate", ".sai", ".sai2", ".mdp", ".xcf", ".afphoto", ".afdesign",
]
# Exports, references and photos: only when she turns this on (art folders can hold thousands).
IMAGE_EXTENSIONS = [
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".heic",
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".eps", ".svg", ".pdf",
]
DEFAULT_EXTENSIONS = list(DRAWING_EXTENSIONS)


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


def default_vault_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "Vault"


def default_size_limit_gb(vault_dir: str) -> float:
    """A quarter of the disk, at most 200 GB: big PSDs add up fast."""
    import shutil

    p = Path(vault_dir)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        total = shutil.disk_usage(p).total / 1024**3
    except OSError:
        return 50
    return float(max(10, min(200, round(total * 0.25))))


def detect_cloud_folders() -> dict[str, Path]:
    """Find folders on this PC that a cloud app keeps in sync."""
    found: dict[str, Path] = {}
    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        p = os.environ.get(var)
        if p and Path(p).is_dir():
            found.setdefault("OneDrive", Path(p))
    home = Path.home()
    for name, candidates in {
        "Dropbox": [home / "Dropbox"],
        "Google Drive": [Path(f"{d}:/My Drive") for d in "GHIJKLMNOPQRSTUVWXYZ"],
        "iCloud Drive": [home / "iCloudDrive"],
    }.items():
        for c in candidates:
            try:
                if c.is_dir():
                    found[name] = c
                    break
            except OSError:
                pass
    return found


@dataclass
class Config:
    watched_folders: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    include_images: bool = False  # also back up PNG/JPG/... exports and references
    # set when an older version backed up images by default; she's asked once about cleaning up
    pending_image_cleanup: bool = False
    vault_dir: str = field(default_factory=lambda: str(default_vault_dir()))
    # Retention: keep every version this many days, then one per day up to keep_daily_days.
    keep_all_days: int = 7
    keep_daily_days: int = 90
    # Last copy of a deleted file is kept this long (days) after it was deleted.
    keep_deleted_days: int = 365
    # Soft cap on vault size in GB (0 = no cap). Oldest extra versions go first;
    # the newest copy of every file is never removed by this cap.
    max_vault_gb: float = 0
    # Keep the hidden auto-save copies Photoshop / Clip Studio make of open documents.
    capture_autosaves: bool = True
    keep_autosave_days: int = 14
    # Ask Photoshop to save a copy of the open document every few minutes.
    ps_snapshots: bool = True
    ps_snapshot_minutes: int = 5
    cloud_enabled: bool = False
    cloud_dir: str = ""
    start_with_windows: bool = True
    auto_update: bool = True
    send_error_reports: bool = True
    paused: bool = False
    first_run_done: bool = False

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        cfg = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (OSError, ValueError):
                pass
        else:
            cfg.max_vault_gb = default_size_limit_gb(cfg.vault_dir)
            clouds = detect_cloud_folders()
            if "OneDrive" in clouds:
                cfg.cloud_enabled = True
                cfg.cloud_dir = str(clouds["OneDrive"] / "ArtSafe Backup")
        cfg.extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.extensions]
        if p.exists() and ".png" in cfg.extensions and not cfg.include_images:
            # 1.0.0 backed up every image by default; switch to drawing files only
            cfg.extensions = list(DRAWING_EXTENSIONS)
            cfg.pending_image_cleanup = True
            cfg.save()
        for ext in REQUIRED_EXTENSIONS:
            if ext not in cfg.extensions:
                cfg.extensions.append(ext)
        return cfg

    def active_extensions(self) -> set[str]:
        exts = set(self.extensions) | set(REQUIRED_EXTENSIONS)
        if self.include_images:
            exts |= set(IMAGE_EXTENSIONS)
        return exts

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def wants(self, path: str | Path) -> bool:
        """True if this file should be backed up."""
        p = Path(path)
        name = p.name
        if name.startswith("~") or name.startswith(".") or name.lower() == "thumbs.db":
            return False
        if p.suffix.lower() not in self.active_extensions():
            return False
        try:
            vault = Path(self.vault_dir).resolve()
            if vault == p.resolve() or vault in p.resolve().parents:
                return False
            if self.cloud_dir:
                cloud = Path(self.cloud_dir).resolve()
                if cloud in p.resolve().parents:
                    return False
        except OSError:
            return False
        return any(_is_under(p, Path(w)) for w in self.watched_folders)


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False
