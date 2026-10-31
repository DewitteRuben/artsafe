"""The app shows when she saved a file, not when ArtSafe happened to copy it."""
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artsafe.config import Config
from artsafe.vault import Vault

MONTH_AGO = time.time() - 30 * 86400


def make(tmp_path):
    art = tmp_path / "Art"
    art.mkdir()
    f = art / "old.psd"
    f.write_bytes(b"8BPS old work")
    os.utime(f, (MONTH_AGO, MONTH_AGO))
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "Vault"))
    return cfg, f


def test_first_backup_of_old_file_shows_its_own_date(tmp_path):
    cfg, f = make(tmp_path)
    v = Vault(cfg)
    v.backup_file(f)  # first scan: copied today
    [row] = v.list_files(False)
    assert abs(row.last_saved - MONTH_AGO) < 2
    assert row.last_backup > time.time() - 60
    [ver] = v.list_versions(row.id)
    assert abs(ver.when - MONTH_AGO) < 2
    v.close()


def test_backups_from_older_versions_get_fixed(tmp_path):
    cfg, f = make(tmp_path)
    v = Vault(cfg)
    v.backup_file(f)
    v.close()
    # make it look like a vault made by 1.0.1 (no saved_at column)
    db = sqlite3.connect(Path(cfg.vault_dir) / "index.db")
    db.executescript("""
        CREATE TABLE v2 AS SELECT id, file_id, blob, size, backed_up_at, reason FROM versions;
        DROP TABLE versions; ALTER TABLE v2 RENAME TO versions;""")
    db.commit()
    db.close()
    v = Vault(cfg)
    [row] = v.list_files(False)
    assert abs(row.last_saved - MONTH_AGO) < 2
    assert abs(v.list_versions(row.id)[0].when - MONTH_AGO) < 2
    v.close()
