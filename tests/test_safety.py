"""Scenarios where a backup tool could lose or damage work."""
import io
import os
import shutil
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

import artsafe.vault as vault_mod
from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.thumbs import _embedded_psd_thumbnail
from artsafe.vault import DiskFull, Vault


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    art = tmp_path / "Art"
    art.mkdir()
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "vault"),
                 include_images=True)  # tests use PNGs as stand-in art
    v = Vault(cfg)
    alerts = []
    e = Engine(cfg, v, on_deleted=alerts.append)
    yield cfg, v, e, art, alerts, tmp_path
    v.close()


bump = [0]


def write(p: Path, color):
    Image.new("RGB", (32, 32), color).save(p, "PNG")
    bump[0] += 2
    t = time.time() + bump[0]
    os.utime(p, (t, t))


def settle(e):
    e.process_pending(settle=0)


def test_photoshop_style_save_keeps_history_and_no_alert(env):
    cfg, v, e, art, alerts, _ = env
    f = art / "dragon.png"
    write(f, "red")
    e.full_scan()
    # app renames the old file away, writes a new one, deletes the old
    tmp_old = art / "dragon_old.png"
    os.rename(f, tmp_old)
    e.handle_move(str(f), str(tmp_old))
    write(f, "blue")
    e.queue(str(f))
    tmp_old.unlink()
    e.queue(str(tmp_old))
    settle(e)
    [row] = v.list_files(False)
    assert row.name == "dragon.png" and row.version_count == 2
    assert alerts == [] and v.list_files(True) == []


def test_real_rename_carries_history(env):
    cfg, v, e, art, alerts, _ = env
    f = art / "a.png"
    write(f, "red")
    e.full_scan()
    write(f, "green")
    e.full_scan()
    g = art / "renamed.png"
    os.rename(f, g)
    e.handle_move(str(f), str(g))
    settle(e)
    [row] = v.list_files(False)
    assert row.name == "renamed.png" and row.version_count == 2 and alerts == []


def test_unplugged_drive_is_not_called_deleted(env):
    cfg, v, e, art, alerts, tmp = env
    write(art / "x.png", "red")
    e.full_scan()
    shutil.move(str(art), str(tmp / "elsewhere"))  # folder disappears, like an unplugged drive
    e.full_scan()
    assert alerts == [] and v.list_files(True) == []


def test_save_copy_over_existing_file_backs_it_up_first(env):
    cfg, v, e, art, alerts, _ = env
    f = art / "a.png"
    write(f, "red")
    old = v.backup_file(f)
    write(f, "blue")  # newer work, not backed up yet
    v.save_copy(old.id, f)  # she picks her current file as the destination by mistake
    blobs = {x.blob for x in v.list_versions(v.list_files(False)[0].id)}
    assert len(blobs) == 2  # the blue version was kept before being replaced


def test_restore_refuses_when_current_file_cant_be_backed_up(env, monkeypatch):
    cfg, v, e, art, alerts, _ = env
    f = art / "a.png"
    write(f, "red")
    old = v.backup_file(f)
    write(f, "blue")
    monkeypatch.setattr(v, "free_space_ok", lambda incoming=0: False)
    with pytest.raises(DiskFull):
        v.restore(old.id)
    assert Image.open(f).getpixel((0, 0)) == (0, 0, 255)  # her current file untouched


def test_low_disk_space_pauses_backups(env, monkeypatch):
    cfg, v, e, art, alerts, _ = env
    monkeypatch.setattr(vault_mod.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(100 * 1024**3, 99 * 1024**3, 1 * 1024**3))
    write(art / "a.png", "red")
    e.full_scan()
    assert e.disk_full and v.list_files(False) == []
    assert "Disk almost full" in e.last_activity


def test_recovery_files_inside_protected_folder_dont_alert(tmp_path, monkeypatch):
    home = tmp_path / "home"
    rec = home / "AppData" / "Roaming" / "Adobe" / "Adobe Photoshop 2026" / "AutoRecover"
    rec.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    cfg = Config(watched_folders=[str(home)], vault_dir=str(tmp_path / "vault"))
    v = Vault(cfg)
    alerts = []
    e = Engine(cfg, v, on_deleted=alerts.append)
    p = rec / "doc.psd"
    p.write_bytes(b"8BPS fake")
    e.full_scan()
    p.unlink()
    e.queue(str(p))
    settle(e)
    assert alerts == [] and len(v.list_autosaves()) == 1
    v.close()


def test_embedded_preview_of_big_psd_is_read_without_loading_the_file(tmp_path):
    jpeg = io.BytesIO()
    Image.new("RGB", (160, 100), "orange").save(jpeg, "JPEG")
    data = b"\0" * 28 + jpeg.getvalue()
    res = b"8BIM" + struct.pack(">H", 1005) + b"\0\0" + struct.pack(">I", 2) + b"xx"
    res += b"8BIM" + struct.pack(">H", 1036) + b"\0\0" + struct.pack(">I", len(data)) + data
    if len(data) % 2:
        res += b"\0"
    header = b"8BPS" + b"\0" * 22
    p = tmp_path / "huge.psb"
    p.write_bytes(header + struct.pack(">I", 0) + struct.pack(">I", len(res)) + res + b"\0" * 1000)
    img = _embedded_psd_thumbnail(p)
    assert img.size == (160, 100)


def test_cloud_copy_follows_rename_and_survives_delete(env):
    cfg, v, e, art, alerts, tmp = env
    cfg.cloud_enabled, cfg.cloud_dir = True, str(tmp / "cloud")
    f = art / "a.png"
    write(f, "red")
    e.full_scan()
    g = art / "b.png"
    os.rename(f, g)
    e.handle_move(str(f), str(g))
    settle(e)
    assert v.cloud_target(g).exists()
    g.unlink()
    e.queue(str(g))
    settle(e)
    assert v.cloud_target(g).exists()  # deleting never removes the cloud copy
