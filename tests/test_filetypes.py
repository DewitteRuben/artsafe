import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.vault import Vault


def test_old_config_switches_to_drawing_files_only(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    old_default = [".psd", ".psb", ".png", ".jpg", ".jpeg", ".tif", ".ai", ".clip", ".pdf"]
    (tmp_path / "ArtSafe").mkdir()
    (tmp_path / "ArtSafe" / "config.json").write_text(json.dumps(
        {"watched_folders": [str(tmp_path / "Art")], "extensions": old_default, "first_run_done": True}))
    cfg = Config.load()
    assert cfg.pending_image_cleanup and ".png" not in cfg.extensions
    assert Config.load().pending_image_cleanup  # saved, so she's asked even after a restart
    art = tmp_path / "Art"
    assert cfg.wants(art / "x.psd") and cfg.wants(art / "x.clip") and cfg.wants(art / "x.kra")
    assert not cfg.wants(art / "export.png") and not cfg.wants(art / "ref.jpg")
    cfg.include_images = True
    assert cfg.wants(art / "export.png")


def test_new_install_has_no_cleanup_question(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = Config.load()
    assert not cfg.pending_image_cleanup and not cfg.include_images


def test_scan_skips_images_and_cleanup_frees_their_backups(tmp_path):
    art = tmp_path / "Art"
    art.mkdir()
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "vault"), include_images=True)
    v = Vault(cfg)
    Image.new("RGB", (300, 300), "red").save(art / "drawing.psd", "PNG")
    for i in range(20):
        Image.new("RGB", (300, 300), (i, 100, 200)).save(art / f"ref{i}.png")
    Image.new("RGB", (300, 300), "blue").save(art / "photo.jpg")
    Engine(cfg, v).full_scan()
    assert len(v.list_files(False)) == 22

    cfg.include_images = False
    files, size = v.unwanted_backup_size(cfg.active_extensions())
    assert files == 21 and size > 0
    blobs_before = len(list(v.blobs.glob("*/*")))
    v.remove_backups_of_other_types(cfg.active_extensions())
    assert [f.name for f in v.list_files(False)] == ["drawing.psd"]
    assert len(list(v.blobs.glob("*/*"))) == blobs_before - 21  # space actually freed
    assert (art / "ref3.png").exists() and (art / "photo.jpg").exists()  # her images untouched

    # with images off, a new scan doesn't bring them back
    Engine(cfg, v).full_scan()
    assert [f.name for f in v.list_files(False)] == ["drawing.psd"]
    v.close()
