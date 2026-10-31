import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from psd_tools import PSDImage

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.vault import Vault


def save_psb(path: Path, color, bump):
    PSDImage.frompil(Image.new("RGB", (80, 60), color)).save(path)
    t = time.time() + bump
    os.utime(path, (t, t))


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    rec = tmp_path / "roaming" / "Adobe" / "Adobe Photoshop 2026" / "AutoRecover"
    rec.mkdir(parents=True)
    (tmp_path / "roaming" / "Adobe" / "Other").mkdir()
    art = tmp_path / "Art"
    art.mkdir()
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "vault"),
                 cloud_enabled=True, cloud_dir=str(tmp_path / "cloud"))
    vault = Vault(cfg)
    deleted = []
    e = Engine(cfg, vault, on_deleted=deleted.append)
    return cfg, vault, e, rec, art, deleted


def test_every_photoshop_autosave_is_kept(tmp_path, monkeypatch):
    cfg, vault, e, rec, art, deleted = setup(tmp_path, monkeypatch)
    f = rec / "dragon_a1b2c3.psb"
    noise = tmp_path / "roaming" / "Adobe" / "Other" / "cache.psb"  # not an AutoRecover folder
    save_psb(noise, "black", 0)
    save_psb(f, "red", 0)
    e.full_scan()
    save_psb(f, "blue", 2)  # Photoshop overwrites its recovery file every few minutes
    e.queue(str(f))
    e.process_pending(settle=0)
    f.unlink()  # Photoshop deletes it when the document is closed
    e.queue(str(f))
    e.process_pending(settle=0)

    saves = vault.list_autosaves()
    assert len(saves) == 2 and all(v.app == "Photoshop" for v in saves)
    assert all(vault.thumb_path(v.blob).exists() for v in saves)
    assert deleted == []  # no "you deleted a file" alert for this
    assert vault.list_files(False) == [] and vault.list_files(True) == []  # not mixed into her files
    assert not (tmp_path / "cloud").exists()  # auto-saves stay local

    out = tmp_path / "recovered.psb"
    vault.save_copy(saves[-1].id, out)
    assert PSDImage.open(out).composite().getpixel((5, 5))[:3] == (255, 0, 0)


def test_autosaves_expire(tmp_path, monkeypatch):
    cfg, vault, e, rec, art, _ = setup(tmp_path, monkeypatch)
    f = rec / "x.psb"
    save_psb(f, "red", 0)
    e.full_scan()
    vault.db.execute("UPDATE versions SET backed_up_at=?", (time.time() - 30 * 86400,))
    vault.db.commit()
    vault.prune()
    assert vault.list_autosaves() == []


def test_autosaves_can_be_turned_off(tmp_path, monkeypatch):
    cfg, vault, e, rec, art, _ = setup(tmp_path, monkeypatch)
    cfg.capture_autosaves = False
    save_psb(rec / "x.psb", "red", 0)
    e.full_scan()
    assert vault.list_autosaves() == []
