"""The backup store.

Every version of a file is copied into <vault>/blobs, named by its SHA-256 so
identical content is only stored once. A small SQLite index records which file
each blob belongs to and when it was saved.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import thumbs
from .config import Config

import logging
log = logging.getLogger(__name__)

CHUNK = 4 * 1024 * 1024
# Never let backups eat the last bit of disk space, or Photoshop couldn't save her work.
MIN_FREE_BYTES = 5 * 1024**3
MIN_FREE_FRACTION = 0.05
# Cloud-only (OneDrive "Files On-Demand") files: reading them would download them. Skip.
CLOUD_ONLY_ATTRS = 0x00400000 | 0x00040000 | 0x00001000  # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
GC_GRACE_SECONDS = 3600


class FileBusy(Exception):
    """The file is locked or still being written; try again later."""


class DiskFull(Exception):
    """Not enough free space left to safely make a backup."""


@dataclass
class FileRow:
    id: int
    path: str
    deleted_at: float | None
    version_count: int
    last_backup: float
    latest_blob: str
    last_saved: float = 0  # when she last saved the file (its modified date), not when ArtSafe copied it

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        return str(Path(self.path).parent)


@dataclass
class VersionRow:
    id: int
    file_id: int
    path: str
    blob: str
    size: int
    backed_up_at: float
    reason: str
    app: str = ""
    saved_at: float | None = None  # the file's modified date when it was copied

    @property
    def when(self) -> float:
        """When this version was saved. Falls back to the backup time for old backups."""
        return self.saved_at or self.backed_up_at


def _is_blob_name(stem: str) -> bool:
    return len(stem) == 64 and all(c in "0123456789abcdef" for c in stem)


def path_key(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


class Vault:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self._open(Path(cfg.vault_dir))

    def _open(self, root: Path) -> None:
        self.root = root
        self.blobs = self.root / "blobs"
        self.thumbs = self.root / "thumbs"
        self.tmp = self.root / "tmp"
        for d in (self.blobs, self.thumbs, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        for leftover in self.tmp.glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        self.db = sqlite3.connect(str(self.root / "index.db"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files(
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                path_key TEXT NOT NULL UNIQUE,
                last_size INTEGER,
                last_mtime REAL,
                deleted_at REAL
            );
            CREATE TABLE IF NOT EXISTS versions(
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id),
                blob TEXT NOT NULL,
                size INTEGER NOT NULL,
                backed_up_at REAL NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS versions_file ON versions(file_id, backed_up_at);
            CREATE INDEX IF NOT EXISTS versions_blob ON versions(blob);
            """
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "kind" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'")
            self.db.execute("ALTER TABLE files ADD COLUMN app TEXT")
        vcols = {r[1] for r in self.db.execute("PRAGMA table_info(versions)")}
        if "saved_at" not in vcols:
            self.db.execute("ALTER TABLE versions ADD COLUMN saved_at REAL")
            # Older backups only know when they were copied. The newest copy of each file
            # matches its last known modified date, so fill that in.
            self.db.execute(
                """UPDATE versions SET saved_at=(SELECT last_mtime FROM files WHERE files.id=versions.file_id)
                   WHERE id IN (SELECT (SELECT id FROM versions v2 WHERE v2.file_id=f.id
                                        ORDER BY backed_up_at DESC, id DESC LIMIT 1) FROM files f)"""
            )
        self.db.commit()

    # ---------- paths ----------
    def blob_path(self, blob: str, ext: str = "") -> Path:
        return self.blobs / blob[:2] / (blob + ext.lower())

    def _blob_file(self, v: VersionRow) -> Path:
        return self.blob_path(v.blob, Path(v.path).suffix)

    def thumb_path(self, blob: str) -> Path:
        return self.thumbs / blob[:2] / (blob + ".jpg")

    # ---------- backing up ----------
    def backup_file(self, path: str | Path, reason: str = "saved",
                    kind: str = "file", app: str | None = None) -> VersionRow | None:
        """Copy the file into the vault if it changed. Returns the new version, or None.

        kind is "file" for her own files, or "autosave" for an app's hidden recovery copy.
        """
        path = Path(path)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise FileBusy(str(e))
        if getattr(st, "st_file_attributes", 0) & CLOUD_ONLY_ATTRS:
            return None  # only stored online; the cloud service already keeps it
        key = path_key(path)
        with self.lock:
            row = self.db.execute(
                "SELECT id, last_size, last_mtime, deleted_at FROM files WHERE path_key=?", (key,)
            ).fetchone()
        if row and row[1] == st.st_size and row[2] == st.st_mtime and row[3] is None:
            if kind == "file":
                self._ensure_cloud_copy(path, row[0])  # e.g. just renamed
            return None  # unchanged since the last backup

        self.check_space(st.st_size)
        tmp = self.tmp / uuid.uuid4().hex
        h = hashlib.sha256()
        try:
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                while chunk := src.read(CHUNK):
                    h.update(chunk)
                    dst.write(chunk)
            st2 = path.stat()
        except FileNotFoundError:
            tmp.unlink(missing_ok=True)
            return None
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise FileBusy(str(e))
        if (st2.st_size, st2.st_mtime) != (st.st_size, st.st_mtime):
            tmp.unlink(missing_ok=True)
            raise FileBusy("file changed while copying")

        blob = h.hexdigest()
        dest = self.blob_path(blob, path.suffix)
        now = time.time()
        # Placing the blob and recording it happen under one lock, so cleanup can never
        # delete a blob in between.
        with self.lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                tmp.unlink(missing_ok=True)
            elif tmp.parent == self.tmp:
                os.replace(tmp, dest)
            else:  # backups were moved to another drive while this file was being copied
                shutil.move(str(tmp), str(dest))
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO files(path, path_key, last_size, last_mtime, kind, app) VALUES(?,?,?,?,?,?)",
                    (str(path), key, st.st_size, st.st_mtime, kind, app),
                )
                file_id = cur.lastrowid
            else:
                file_id = row[0]
                self.db.execute(
                    "UPDATE files SET path=?, last_size=?, last_mtime=?, deleted_at=NULL WHERE id=?",
                    (str(path), st.st_size, st.st_mtime, file_id),
                )
            latest = self.db.execute(
                "SELECT blob FROM versions WHERE file_id=? ORDER BY backed_up_at DESC, id DESC LIMIT 1",
                (file_id,),
            ).fetchone()
            if latest and latest[0] == blob:
                self.db.commit()
                same = True
            else:
                same = False
        if same:
            # same content as the newest copy (e.g. renamed, or only the date changed)
            if kind == "file":
                target = self.cloud_target(path)
                if target is not None and not target.exists():
                    self.mirror_to_cloud(path, dest)
            return None
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO versions(file_id, blob, size, backed_up_at, reason, saved_at) VALUES(?,?,?,?,?,?)",
                (file_id, blob, st.st_size, now, reason, st.st_mtime),
            )
            self.db.commit()
            version = VersionRow(cur.lastrowid, file_id, str(path), blob, st.st_size, now, reason, app or "",
                                 st.st_mtime)

        tp = self.thumb_path(blob)
        if not tp.exists():
            thumbs.make_thumbnail(dest, tp)
        if kind == "file":
            self.mirror_to_cloud(path, dest)
        return version

    def mark_deleted(self, path: str | Path) -> FileRow | None:
        """Record that a file disappeared. Returns the file if it was newly marked."""
        if Path(path).exists():
            return None
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM files WHERE path_key=? AND deleted_at IS NULL", (path_key(path),)
            ).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE files SET deleted_at=? WHERE id=?", (time.time(), row[0]))
            self.db.commit()
        return self.get_file(row[0])

    def rename(self, src: str | Path, dest: str | Path) -> bool:
        """Carry history over when a file is renamed or moved. False if not applicable."""
        with self.lock:
            src_row = self.db.execute(
                "SELECT id FROM files WHERE path_key=?", (path_key(src),)
            ).fetchone()
            dest_row = self.db.execute(
                "SELECT id FROM files WHERE path_key=?", (path_key(dest),)
            ).fetchone()
            if not src_row or dest_row:
                return False
            self.db.execute(
                "UPDATE files SET path=?, path_key=?, deleted_at=NULL WHERE id=?",
                (str(dest), path_key(dest), src_row[0]),
            )
            self.db.commit()
            return True

    def known_paths_under(self, folder: str | Path, include_deleted: bool = False) -> list[str]:
        prefix = path_key(folder).rstrip("\\/") + os.sep
        q = "SELECT path, path_key FROM files" + ("" if include_deleted else " WHERE deleted_at IS NULL")
        with self.lock:
            rows = self.db.execute(q).fetchall()
        return [p for p, k in rows if k.startswith(prefix)]

    # ---------- reading ----------
    def list_files(self, deleted: bool, search: str = "") -> list[FileRow]:
        q = """
            SELECT f.id, f.path, f.deleted_at, COUNT(v.id), MAX(v.backed_up_at),
                   (SELECT blob FROM versions WHERE file_id=f.id ORDER BY backed_up_at DESC, id DESC LIMIT 1),
                   MAX(COALESCE(v.saved_at, v.backed_up_at))
            FROM files f JOIN versions v ON v.file_id=f.id
            WHERE f.deleted_at IS {} NULL AND f.kind='file'
            GROUP BY f.id
        """.format("NOT" if deleted else "")
        with self.lock:
            rows = self.db.execute(q).fetchall()
        out = [FileRow(*r) for r in rows]
        if search:
            s = search.lower()
            out = [f for f in out if s in f.path.lower()]
        sort_key = (lambda f: f.deleted_at or 0) if deleted else (lambda f: f.last_saved)
        out.sort(key=sort_key, reverse=True)
        return out

    def list_autosaves(self, search: str = "") -> list[VersionRow]:
        """Every kept auto-save copy, newest first."""
        with self.lock:
            rows = self.db.execute(
                """SELECT v.id, v.file_id, f.path, v.blob, v.size, v.backed_up_at, v.reason, COALESCE(f.app, ''), v.saved_at
                   FROM versions v JOIN files f ON f.id=v.file_id
                   WHERE f.kind='autosave' ORDER BY COALESCE(v.saved_at, v.backed_up_at) DESC, v.id DESC"""
            ).fetchall()
        out = [VersionRow(*r) for r in rows]
        if search:
            s = search.lower()
            out = [v for v in out if s in v.path.lower() or s in v.app.lower()]
        return out

    def get_file(self, file_id: int) -> FileRow | None:
        with self.lock:
            r = self.db.execute(
                """SELECT f.id, f.path, f.deleted_at, COUNT(v.id), MAX(v.backed_up_at),
                   (SELECT blob FROM versions WHERE file_id=f.id ORDER BY backed_up_at DESC, id DESC LIMIT 1),
                   MAX(COALESCE(v.saved_at, v.backed_up_at))
                   FROM files f JOIN versions v ON v.file_id=f.id WHERE f.id=? GROUP BY f.id""",
                (file_id,),
            ).fetchone()
        return FileRow(*r) if r else None

    def list_versions(self, file_id: int) -> list[VersionRow]:
        with self.lock:
            rows = self.db.execute(
                """SELECT v.id, v.file_id, f.path, v.blob, v.size, v.backed_up_at, v.reason, COALESCE(f.app, ''), v.saved_at
                   FROM versions v JOIN files f ON f.id=v.file_id
                   WHERE v.file_id=? ORDER BY v.backed_up_at DESC, v.id DESC""",
                (file_id,),
            ).fetchall()
        return [VersionRow(*r) for r in rows]

    def get_version(self, version_id: int) -> VersionRow | None:
        with self.lock:
            r = self.db.execute(
                """SELECT v.id, v.file_id, f.path, v.blob, v.size, v.backed_up_at, v.reason, COALESCE(f.app, ''), v.saved_at
                   FROM versions v JOIN files f ON f.id=v.file_id WHERE v.id=?""",
                (version_id,),
            ).fetchone()
        return VersionRow(*r) if r else None

    def stats(self) -> dict:
        with self.lock:
            files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            versions = self.db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            size = self.db.execute(
                "SELECT COALESCE(SUM(size),0) FROM (SELECT blob, MAX(size) AS size FROM versions GROUP BY blob)"
            ).fetchone()[0]
        return {"files": files, "versions": versions, "bytes": size}

    # ---------- restoring ----------
    def restore(self, version_id: int, target: str | Path | None = None) -> Path:
        """Put a version back. Whatever is at the target now is backed up first."""
        v = self.get_version(version_id)
        log.info("Restore requested: version %s of %s", version_id, v.path if v else "?")
        if v is None:
            raise ValueError("version not found")
        target = Path(target) if target else Path(v.path)
        if target.exists():
            self.backup_file(target, reason="before restore")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.artsafe-restore")
        shutil.copyfile(self._blob_file(v), tmp)
        os.replace(tmp, target)
        self.backup_file(target, reason="restored")
        return target

    def save_copy(self, version_id: int, dest: str | Path) -> Path:
        v = self.get_version(version_id)
        if v is None:
            raise ValueError("version not found")
        dest = Path(dest)
        if dest.exists():
            self.backup_file(dest, reason="before restore")  # in case she picked an existing file
        tmp = dest.with_name(f".{dest.name}.artsafe-copy")
        shutil.copyfile(self._blob_file(v), tmp)
        os.replace(tmp, dest)
        return dest

    def free_space_ok(self, incoming: int = 0) -> bool:
        try:
            du = shutil.disk_usage(self.root)
        except OSError:
            return True
        keep = max(MIN_FREE_BYTES, du.total * MIN_FREE_FRACTION)
        return du.free - incoming > keep

    def check_space(self, incoming: int) -> None:
        if not self.free_space_ok(incoming):
            raise DiskFull("Not enough free disk space to make backups")

    def version_file(self, version_id: int) -> Path | None:
        v = self.get_version(version_id)
        return self._blob_file(v) if v else None

    # ---------- cloud ----------
    def cloud_target(self, original: str | Path) -> Path | None:
        if not (self.cfg.cloud_enabled and self.cfg.cloud_dir):
            return None
        p = Path(os.path.abspath(str(original)))
        try:
            # files in her user folder keep a familiar layout: "ArtSafe Backup\Desktop\Commissions\..."
            return Path(self.cfg.cloud_dir) / p.relative_to(Path.home())
        except ValueError:
            drive = p.drive.rstrip(":\\/").replace("\\", "_") or "root"
            return Path(self.cfg.cloud_dir) / f"Drive {drive}" / p.relative_to(p.anchor)

    def mirror_to_cloud(self, original: str | Path, blob_file: Path) -> None:
        """Keep the newest copy of each file in the cloud-synced folder. Deletions are never copied."""
        target = self.cloud_target(original)
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".artsafe-tmp")
            shutil.copyfile(blob_file, tmp)
            os.replace(tmp, target)
        except OSError as e:
            log.warning("Cloud copy of %s failed (%s); will retry later", original, e)

    def _ensure_cloud_copy(self, path: Path, file_id: int) -> None:
        target = self.cloud_target(path)
        if target is None or target.exists():
            return
        with self.lock:
            latest = self.db.execute(
                "SELECT blob FROM versions WHERE file_id=? ORDER BY backed_up_at DESC, id DESC LIMIT 1",
                (file_id,),
            ).fetchone()
        if latest:
            self.mirror_to_cloud(path, self.blob_path(latest[0], path.suffix))

    def sync_cloud_all(self) -> int:
        """Make sure the cloud folder has the newest copy of every file. Returns files copied."""
        if not (self.cfg.cloud_enabled and self.cfg.cloud_dir):
            return 0
        copied = 0
        for deleted in (False, True):
            for f in self.list_files(deleted=deleted):
                target = self.cloud_target(f.path)
                src = self.blob_path(f.latest_blob, Path(f.path).suffix)
                try:
                    if target.exists() and target.stat().st_size == src.stat().st_size:
                        continue
                except OSError:
                    continue
                self.mirror_to_cloud(f.path, src)
                copied += 1
        return copied

    # ---------- cleanup ----------
    def prune(self, now: float | None = None) -> int:
        """Apply the retention rules. Returns how many versions were removed."""
        now = now or time.time()
        day = 86400
        cfg = self.cfg
        drop: list[int] = []
        with self.lock:
            files = self.db.execute("SELECT id, deleted_at, kind FROM files").fetchall()
            for file_id, deleted_at, kind in files:
                vs = self.db.execute(
                    "SELECT id, backed_up_at FROM versions WHERE file_id=? ORDER BY backed_up_at DESC, id DESC",
                    (file_id,),
                ).fetchall()
                if kind == "autosave":
                    drop.extend(vid for vid, t in vs if now - t > cfg.keep_autosave_days * day)
                    continue
                if deleted_at and now - deleted_at > cfg.keep_deleted_days * day:
                    drop.extend(v[0] for v in vs)
                    continue
                seen_days: set[str] = set()
                for i, (vid, t) in enumerate(vs):
                    age = now - t
                    if i == 0 or age < cfg.keep_all_days * day:
                        continue
                    if age < cfg.keep_daily_days * day:
                        d = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
                        if d not in seen_days:
                            seen_days.add(d)
                            continue
                    drop.append(vid)
            self._delete_versions(drop)

            if cfg.max_vault_gb and cfg.max_vault_gb > 0:
                cap = cfg.max_vault_gb * 1024**3
                extra = self.db.execute(
                    """SELECT v.id FROM versions v
                       WHERE v.id NOT IN (
                         SELECT (SELECT id FROM versions WHERE file_id=f.id
                                 ORDER BY backed_up_at DESC, id DESC LIMIT 1) FROM files f)
                       ORDER BY v.backed_up_at ASC"""
                ).fetchall()
                for (vid,) in extra:
                    if self.stats()["bytes"] <= cap:
                        break
                    self._delete_versions([vid])
                    drop.append(vid)

            self.db.execute("DELETE FROM files WHERE id NOT IN (SELECT DISTINCT file_id FROM versions)")
            self.db.commit()
        self._collect_garbage()
        return len(drop)

    def _delete_versions(self, ids: list[int]) -> None:
        for vid in ids:
            self.db.execute("DELETE FROM versions WHERE id=?", (vid,))

    def _collect_garbage(self, grace: float | None = None) -> None:
        cutoff = time.time() - (GC_GRACE_SECONDS if grace is None else grace)
        with self.lock:
            used = {r[0] for r in self.db.execute("SELECT DISTINCT blob FROM versions")}
            for folder in (self.blobs, self.thumbs):
                for f in folder.glob("*/*"):
                    # Only ever ArtSafe's own copies: named by their SHA-256, inside blobs/ or thumbs/.
                    if f.stem in used or not _is_blob_name(f.stem) or f.parent.parent != folder:
                        continue
                    try:
                        if f.stat().st_mtime < cutoff:  # extra safety: never touch fresh files
                            f.unlink()
                    except OSError:
                        pass

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ---------- file types she no longer wants backed up ----------
    def _file_ids_not_matching(self, keep_exts: set[str]) -> list[int]:
        with self.lock:
            rows = self.db.execute("SELECT id, path FROM files WHERE kind='file'").fetchall()
        return [i for i, p in rows if Path(p).suffix.lower() not in keep_exts]

    def unwanted_backup_size(self, keep_exts: set[str]) -> tuple[int, int]:
        """(files, bytes that removing their backups would free) for types outside keep_exts."""
        return self._backup_size(self._file_ids_not_matching(keep_exts))

    def _backup_size(self, ids: list[int]) -> tuple[int, int]:
        if not ids:
            return 0, 0
        with self.lock:
            marks = ",".join("?" * len(ids))
            blobs = {r[0]: r[1] for r in self.db.execute(
                f"SELECT blob, MAX(size) FROM versions WHERE file_id IN ({marks}) GROUP BY blob", ids)}
            shared = {r[0] for r in self.db.execute(
                f"SELECT DISTINCT blob FROM versions WHERE file_id NOT IN ({marks})", ids)}
        return len(ids), sum(size for b, size in blobs.items() if b not in shared)

    def remove_backups_of_other_types(self, keep_exts: set[str]) -> int:
        """Delete backups of files whose type isn't in keep_exts. Her own files aren't touched."""
        ids = self._file_ids_not_matching(keep_exts)
        self._forget(ids)
        log.info("Removed backups of %d files of types she no longer backs up", len(ids))
        return len(ids)

    # ---------- folders she no longer protects ----------
    def _file_ids_only_under(self, folder: str | Path, still_watched: list[str]) -> list[int]:
        """Her files (not app auto-saves) inside folder, except ones another watched folder still covers."""
        def prefix(d):
            return path_key(d).rstrip("\\/") + os.sep
        gone = prefix(folder)
        keep = [prefix(w) for w in still_watched]
        with self.lock:
            rows = self.db.execute("SELECT id, path_key FROM files WHERE kind='file'").fetchall()
        return [i for i, k in rows if k.startswith(gone) and not any(k.startswith(w) for w in keep)]

    def folder_backup_size(self, folder: str | Path, still_watched: list[str]) -> tuple[int, int]:
        """(files, bytes freed) if the backups of a folder she stopped protecting were removed."""
        return self._backup_size(self._file_ids_only_under(folder, still_watched))

    def remove_folder_backups(self, folder: str | Path, still_watched: list[str]) -> int:
        """Delete ArtSafe's copies of files in a folder she stopped protecting.
        Only touches the backup folder; the folder and her files in it are never touched."""
        ids = self._file_ids_only_under(folder, still_watched)
        self._forget(ids)
        log.info("Removed backups of %d files from a folder she no longer protects", len(ids))
        return len(ids)

    def _forget(self, ids: list[int]) -> None:
        """Drop these files' backups from the index, then free the copies nothing else uses."""
        with self.lock:
            for i in ids:
                self.db.execute("DELETE FROM versions WHERE file_id=?", (i,))
                self.db.execute("DELETE FROM files WHERE id=?", (i,))
            self.db.commit()
        self._collect_garbage(grace=0)  # unreferenced on purpose: free the space right away

    # ---------- moving to another folder / drive ----------
    def size_on_disk(self) -> tuple[int, int]:
        """(files, bytes) in the backup folder."""
        n = total = 0
        for f in self.root.rglob("*"):
            if f.is_file() and f.parent != self.tmp:
                n += 1
                total += f.stat().st_size
        return n, total

    def check_new_location(self, new_root: Path) -> str | None:
        """A reason the folder can't be used, or None if it's fine."""
        new_root = Path(os.path.abspath(new_root))
        old = Path(os.path.abspath(self.root))
        if path_key(new_root) == path_key(old):
            return "That's already where your backups are."
        if path_key(old) in [path_key(p) for p in new_root.parents] or \
                path_key(new_root) in [path_key(p) for p in old.parents]:
            return "Pick a folder that isn't inside (or around) the current backup folder."
        if self.cfg.cloud_dir:
            cloud = Path(os.path.abspath(self.cfg.cloud_dir))
            if path_key(new_root) == path_key(cloud) or path_key(cloud) in [path_key(p) for p in new_root.parents]:
                return "Don't put backups inside the cloud folder: it would upload every old version."
        if new_root.exists() and any(new_root.iterdir()):
            return "Pick an empty folder (or let ArtSafe create one)."
        anchor = Path(new_root.anchor)
        if not anchor.exists():
            return f"Drive {new_root.anchor} isn't available."
        try:
            new_root.mkdir(parents=True, exist_ok=True)
            probe = new_root / ".artsafe-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            return f"ArtSafe can't write there ({e.strerror or e})."
        _, need = self.size_on_disk()
        free = shutil.disk_usage(new_root).free
        if free - need < MIN_FREE_BYTES:
            return (f"Not enough space there: backups need {need / 1024**3:.1f} GB and that drive has "
                    f"{free / 1024**3:.1f} GB free (ArtSafe keeps at least 5 GB free).")
        return None

    def relocate(self, new_root: str | Path, progress=lambda done, total: None) -> Path:
        """Move all backups to new_root. Copies and checks everything before deleting the old copy.

        Backups wait while this runs. On any error the old location stays in use, untouched.
        """
        new_root = Path(os.path.abspath(new_root))
        problem = self.check_new_location(new_root)
        if problem:
            raise ValueError(problem)
        old_root = self.root
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
            try:
                files = [f for f in old_root.rglob("*") if f.is_file() and f.parent != self.tmp]
                total = sum(f.stat().st_size for f in files)
                done = 0
                for f in files:
                    target = new_root / f.relative_to(old_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                    if target.stat().st_size != f.stat().st_size:
                        raise OSError(f"copy of {f.name} is incomplete")
                    done += f.stat().st_size
                    progress(done, total)
            except BaseException:
                shutil.rmtree(new_root, ignore_errors=True)
                self._open(old_root)
                raise
            self._open(new_root)
            self.cfg.vault_dir = str(new_root)
        shutil.rmtree(old_root, ignore_errors=True)  # everything is safely in the new place
        return new_root
