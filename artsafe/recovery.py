"""Where art apps keep their hidden backup / recovery copies of documents.

Photoshop writes a recovery .psb for every open document every few minutes
(Preferences > File Handling), overwrites it each time, and deletes it when the
document is closed.

Clip Studio Paint keeps backups in CELSYSUserData\\CELSYS\\CLIPStudioPaintData:
  DocumentBackup  - the previous version each time a file is saved over,
                    named "<yyyymmddhhmmss><file name>", e.g. 20261031153212Spyx_Edit2026.psd
  InitialBackup   - a copy from when a file was first opened/created,
                    named "<folder name><file name>", e.g. "2026 PhotoshopSilver_halloween2026.psd"
  RecoveryBackup  - canvas-recovery data (when "Enable canvas recovery" is on)
These include .psd files when she edits Photoshop files in Clip Studio. Older
versions used Documents\\CELSYS*\\CLIPStudioPaintData instead.

Apps clean these up themselves; ArtSafe keeps every copy for a while, so work
that was never saved, or later saved over, can be recovered.
"""
from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from pathlib import Path

CLIP_FOLDER_WORDS = ("backup", "recover", "autosave")
# formats Clip Studio can save in
CLIP_EXTENSIONS = (".clip", ".psd", ".psb", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".tga", ".webp", ".lip")


@dataclass(frozen=True)
class Source:
    root: str
    app: str
    extensions: tuple[str, ...]  # empty = any file
    require_any: tuple[str, ...] = ()  # one of these must appear in a folder name in the path

    def matches(self, path: str | Path) -> bool:
        p = Path(path)
        if self.extensions and p.suffix.lower() not in self.extensions:
            return False
        try:
            rel = p.resolve().relative_to(Path(self.root).resolve())
        except (ValueError, OSError):
            return False
        if self.require_any:
            folders = [part.lower() for part in rel.parts[:-1]]
            return any(w in f for f in folders for w in self.require_any)
        return True


def clip_studio_roots() -> list[Path]:
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "CELSYSUserData")
    docs = Path.home() / "Documents"
    try:
        roots += [d for d in docs.glob("CELSYS*") if d.is_dir()]  # versions before 1.10.13
    except OSError:
        pass
    return [r for r in roots if r.is_dir()]


def find_sources() -> list[Source]:
    out: list[Source] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        adobe = Path(appdata) / "Adobe"
        if adobe.is_dir():
            # covers every Photoshop version, including ones installed later
            out.append(Source(str(adobe), "Photoshop", (".psb", ".psd"), ("autorecover",)))
    for root in clip_studio_roots():
        # recovery data: format isn't documented, so keep every file in it
        out.append(Source(str(root), "Clip Studio", (), ("recoverybackup",)))
        # backup folders only: the same area also holds brushes, materials and templates
        out.append(Source(str(root), "Clip Studio", CLIP_EXTENSIONS, CLIP_FOLDER_WORDS))
    # Photoshop can also put them in PSAutoRecover on the scratch disk
    for letter in string.ascii_uppercase[2:]:
        d = Path(f"{letter}:\\PSAutoRecover")
        try:
            if d.is_dir():
                out.append(Source(str(d), "Photoshop", (".psb", ".psd")))
        except OSError:
            pass
    return out


def match(sources: list[Source], path: str | Path) -> Source | None:
    for s in sources:
        if s.matches(path):
            return s
    return None


# ---------- friendly names for the "Unsaved work" list ----------
_STAMP = re.compile(
    r"""(
        [\s_\-.]*\(?\d{4}[-_.]?\d{2}[-_.]?\d{2}([\s_\-T.]?\d{2}[-_.:]?\d{2}([-_.:]?\d{2})?)?\)?   # dates / times
      | [\s_\-.]*\(?\d{6,}\)?                                                                   # long numbers
      | [\s_\-.]+[0-9a-fA-F]{6,}                                                               # random ids
      | [\s_\-.]*\(\d{1,3}\)                                                                    # (1), (2)
    )+$""",
    re.VERBOSE,
)


_LEADING_STAMP = re.compile(r"^\d{14}")  # Clip Studio: 20261031153212Spyx_Edit2026.psd


def artwork_name(path: str | Path, known_names: set[str] | frozenset[str] = frozenset()) -> str:
    """Best guess at the artwork's name.

    'dragon_20261031_153012.clip' -> 'dragon', '20261031153212Spyx_Edit2026.psd' -> 'Spyx_Edit2026'.
    known_names (file names of her own files) resolve Clip Studio's "<folder><file>" names:
    '2026 PhotoshopSilver_halloween2026.psd' -> 'Silver_halloween2026'.
    """
    p = Path(path)
    filename = _LEADING_STAMP.sub("", p.name) if len(p.stem) > 14 else p.name
    lower = filename.lower()
    best = ""
    for k in known_names:
        if lower.endswith(k.lower()) and len(k) > len(best):
            best = k
    if best:
        return Path(best).stem
    stem = Path(filename).stem
    cleaned = _STAMP.sub("", stem).strip(" _-.")
    return cleaned or stem


def describe(path: str | Path, app: str,
             known_names: set[str] | frozenset[str] = frozenset()) -> tuple[str, str]:
    """(artwork name, what kind of copy this is) for an app's backup file."""
    folders = [p.lower() for p in Path(path).parts[:-1]]
    name = artwork_name(path, known_names)
    if app == "Clip Studio":
        if any("documentbackup" in f for f in folders):
            return name, "Clip Studio backup from a save"
        if any("initialbackup" in f for f in folders):
            return name, "Clip Studio copy from when it was first opened"
        return name, "Clip Studio recovery copy"
    if app == "Photoshop":
        return name, "Photoshop auto-save"
    return name, f"{app} copy"
