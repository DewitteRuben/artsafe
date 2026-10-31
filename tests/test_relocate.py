import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

import artsafe.vault as vault_mod
from artsafe.config import Config
from artsafe.vault import Vault

bump = [0]


def write(p, color):
    Image.new("RGB", (40, 30), color).save(p, "PNG")
    bump[0] += 2
    t = time.time() + bump[0]
    os.utime(p, (t, t))


@pytest.fixture
def env(tmp_path):
    art = tmp_path / "Art"
    art.mkdir()
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "C" / "Vault"),
                 cloud_enabled=True, cloud_dir=str(tmp_path / "OneDrive" / "ArtSafe Backup"))
    v = Vault(cfg)
    f = art / "dragon.png"
    for c in ("red", "green", "blue"):
        write(f, c)
        v.backup_file(f)
    yield cfg, v, f, tmp_path
    v.close()


def test_move_keeps_everything_and_keeps_working(env):
    cfg, v, f, tmp = env
    old = Path(cfg.vault_dir)
    new = tmp / "D" / "ArtSafe Backups"
    before = [x.blob for x in v.list_versions(v.list_files(False)[0].id)]
    seen = []
    v.relocate(new, progress=lambda d, t: seen.append((d, t)))
    assert cfg.vault_dir == str(new) and not old.exists()
    assert seen and seen[-1][0] == seen[-1][1]  # progress reached 100%
    [row] = v.list_files(False)
    assert [x.blob for x in v.list_versions(row.id)] == before  # full history intact
    assert all(v.thumb_path(b).exists() for b in before)
    write(f, "white")  # new backups go to the new place
    v.backup_file(f)
    assert len(v.list_versions(row.id)) == 4
    assert len(list((new / "blobs").glob("*/*"))) == 4
    v.restore(v.list_versions(row.id)[-1].id)  # and restoring still works
    assert Image.open(f).getpixel((0, 0)) == (255, 0, 0)


def test_failure_halfway_changes_nothing(env, monkeypatch):
    cfg, v, f, tmp = env
    old = cfg.vault_dir
    new = tmp / "D" / "ArtSafe Backups"
    real_copy = shutil.copy2
    calls = [0]

    def flaky(a, b, *k, **kw):
        calls[0] += 1
        if calls[0] == 3:
            raise OSError("drive unplugged")
        return real_copy(a, b, *k, **kw)

    monkeypatch.setattr(vault_mod.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        v.relocate(new)
    assert cfg.vault_dir == old and not new.exists()
    [row] = v.list_files(False)
    assert len(v.list_versions(row.id)) == 3  # still fully usable from the old place
    write(f, "black")
    assert v.backup_file(f)


def test_refuses_bad_destinations(env, monkeypatch):
    cfg, v, f, tmp = env
    assert "already" in v.check_new_location(Path(cfg.vault_dir))
    assert "inside" in v.check_new_location(Path(cfg.vault_dir) / "sub")
    assert "cloud" in v.check_new_location(Path(cfg.cloud_dir) / "Backups")
    busy = tmp / "Busy"
    busy.mkdir()
    (busy / "x.txt").write_text("hi")
    assert "empty" in v.check_new_location(busy)
    monkeypatch.setattr(vault_mod.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(10 * 1024**3, 9 * 1024**3, 1 * 1024**3))
    assert "Not enough space" in v.check_new_location(tmp / "D" / "ArtSafe Backups")
    assert v.check_new_location(tmp / "E" / "fine") is not None  # still refused: no space
