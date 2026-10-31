import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.vault import Vault

DAY = 86400


@pytest.fixture
def env(tmp_path):
    art = tmp_path / "Art"
    art.mkdir()
    cfg = Config(
        watched_folders=[str(art)],
        vault_dir=str(tmp_path / "vault"),
        cloud_enabled=True,
        cloud_dir=str(tmp_path / "cloud"),
        include_images=True,  # tests use PNGs as stand-in art
    )
    v = Vault(cfg)
    yield cfg, v, art, tmp_path
    v.close()


def write_img(p: Path, color):
    Image.new("RGB", (64, 64), color).save(p)
    # make sure mtime moves forward even on coarse clocks
    t = time.time() + write_img.bump
    write_img.bump += 2
    os.utime(p, (t, t))


write_img.bump = 0


def test_overwrite_keeps_old_versions_and_restore(env):
    cfg, v, art, _ = env
    f = art / "cat.png"
    write_img(f, "red")
    assert v.backup_file(f)
    write_img(f, "blue")
    assert v.backup_file(f)
    assert v.backup_file(f) is None  # unchanged -> no new version

    [row] = v.list_files(deleted=False)
    versions = v.list_versions(row.id)
    assert len(versions) == 2
    oldest = versions[-1]
    assert v.thumb_path(oldest.blob).exists()

    v.restore(oldest.id)
    assert Image.open(f).getpixel((0, 0)) == (255, 0, 0)
    # the blue version is still there, plus a "restored" entry
    blobs = [x.blob for x in v.list_versions(row.id)]
    assert len(set(blobs)) == 2 and len(blobs) == 3


def test_delete_then_restore(env):
    cfg, v, art, _ = env
    f = art / "sub" / "dog.png"
    f.parent.mkdir()
    write_img(f, "green")
    v.backup_file(f)
    f.unlink()
    assert v.mark_deleted(f)
    assert v.list_files(deleted=False) == []
    [row] = v.list_files(deleted=True)
    v.restore(v.list_versions(row.id)[0].id)
    assert f.exists()
    assert v.list_files(deleted=True) == []


def test_cloud_mirror_keeps_deleted(env):
    cfg, v, art, tmp = env
    f = art / "bird.png"
    write_img(f, "yellow")
    v.backup_file(f)
    target = v.cloud_target(f)
    assert target.exists() and str(target).startswith(str(tmp / "cloud"))
    f.unlink()
    v.mark_deleted(f)
    assert target.exists()


def test_prune_rules(env, monkeypatch):
    import artsafe.vault as vault_mod
    monkeypatch.setattr(vault_mod, "GC_GRACE_SECONDS", -10)
    cfg, v, art, _ = env
    f = art / "x.png"
    for c in ["red", "green", "blue", "white"]:
        write_img(f, c)
        v.backup_file(f)
    [row] = v.list_files(deleted=False)
    ids = [x.id for x in v.list_versions(row.id)]  # newest first
    now = time.time()
    ages = [0, 20 * DAY, 20 * DAY + 60, 200 * DAY]  # two on the same day, one very old
    for vid, age in zip(ids, ages):
        v.db.execute("UPDATE versions SET backed_up_at=? WHERE id=?", (now - age, vid))
    v.db.commit()
    removed = v.prune(now)
    left = v.list_versions(row.id)
    assert removed == 2 and len(left) == 2
    # removed blobs are cleaned off disk
    assert len(list(v.blobs.glob("*/*"))) == 2


def test_engine_scan_rename_and_delete(env):
    cfg, v, art, _ = env
    a = art / "a.png"
    write_img(a, "red")
    (art / "notes.txt").write_text("ignored")
    e = Engine(cfg, v)
    e.full_scan()
    assert [f.name for f in v.list_files(False)] == ["a.png"]

    b = art / "b.png"
    os.rename(a, b)
    e.handle_move(str(a), str(b))
    e.process_pending(settle=0)
    files = v.list_files(False)
    assert [f.name for f in files] == ["b.png"] and v.list_files(True) == []

    b.unlink()
    e.queue(str(b))
    e.process_pending(settle=0)
    assert [f.name for f in v.list_files(True)] == ["b.png"]


def test_cloud_layout_is_easy_to_browse(env, monkeypatch):
    cfg, v, art, tmp = env
    monkeypatch.setenv("USERPROFILE", str(tmp))
    assert v.cloud_target(tmp / "Art" / "x.psd") == tmp / "cloud" / "Art" / "x.psd"
    assert v.cloud_target(Path("D:/Projects/y.psd")) == tmp / "cloud" / "Drive D" / "Projects" / "y.psd"
