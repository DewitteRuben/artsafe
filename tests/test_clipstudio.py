import io
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.recovery import artwork_name, describe
from artsafe.vault import Vault


def make_clip(path: Path, color):
    """A .clip like Clip Studio writes: chunks with an SQLite database holding a canvas preview."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = path.with_suffix(".tmpdb")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE CanvasPreview(ImageData BLOB)")
    b = io.BytesIO()
    Image.new("RGB", (120, 90), color).save(b, "PNG")
    con.execute("INSERT INTO CanvasPreview VALUES(?)", (b.getvalue(),))
    con.commit()
    con.close()
    data = db.read_bytes()
    db.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"CSFCHUNK" + b"\0" * 32 + b"CHNKSQLi" + struct.pack(">Q", len(data)) + data + b"CHNKFoot")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    csp = home / "AppData" / "Roaming" / "CELSYSUserData" / "CELSYS" / "CLIPStudioPaintData"
    csp.mkdir(parents=True)
    art = home / "Art"
    art.mkdir()
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "vault"))
    v = Vault(cfg)
    alerts = []
    e = Engine(cfg, v, on_deleted=alerts.append)
    yield v, e, csp, home, alerts
    v.close()


def test_clip_studio_backups_are_kept_with_names(env):
    v, e, csp, home, alerts = env
    make_clip(csp / "DocumentBackup" / "dragon_20261031_153012.clip", "red")
    make_clip(csp / "DocumentBackup" / "dragon_20261031_160544.clip", "blue")
    make_clip(csp / "InitialBackup" / "fox.clip", "green")
    make_clip(csp / "Material" / "Brushes" / "not_art.clip", "black")  # brushes/materials are ignored
    e.full_scan()
    saves = v.list_autosaves()
    assert len(saves) == 3 and all(s.app == "Clip Studio" for s in saves)
    described = sorted(describe(s.path, s.app) for s in saves)
    assert described == [
        ("dragon", "Clip Studio backup from a save"),
        ("dragon", "Clip Studio backup from a save"),
        ("fox", "Clip Studio copy from when it was first opened"),
    ]
    assert all(v.thumb_path(s.blob).exists() for s in saves)  # previews work


def test_clip_studio_cleaning_up_its_backups_is_not_an_alert(env):
    v, e, csp, home, alerts = env
    f = csp / "DocumentBackup" / "dragon_20261031_153012.clip"
    make_clip(f, "red")
    e.full_scan()
    f.unlink()  # Clip Studio removes old backups by itself
    e.queue(str(f))
    e.process_pending(settle=0)
    assert alerts == [] and len(v.list_autosaves()) == 1  # ArtSafe still has it


def test_older_clip_studio_location_in_documents(env):
    v, e, csp, home, alerts = env
    make_clip(home / "Documents" / "CELSYS_EN" / "CLIPStudioPaintData" / "DocumentBackup" / "cat.clip", "red")
    e.full_scan()
    [s] = v.list_autosaves()
    assert describe(s.path, s.app)[0] == "cat"


@pytest.mark.parametrize("name,expected", [
    ("20261031153212Spyx_Edit2026.psd", "Spyx_Edit2026"),
    ("20261028164846Scrungly_Commission_Sketch.clip", "Scrungly_Commission_Sketch"),
    ("dragon_20261031_153012.clip", "dragon"),
    ("dragon 2026-10-31 15.30.12.clip", "dragon"),
    ("my comic page 3.clip", "my comic page 3"),
    ("portrait (2).clip", "portrait"),
    ("fox_8f3a9c1d.psb", "fox"),
    ("20261031.clip", "20261031"),
])
def test_artwork_names(name, expected):
    assert artwork_name(name) == expected


def test_her_real_clip_studio_folders(env):
    """Layout and names copied from her PC (CLIPStudioPaintData screenshots)."""
    from PIL import Image as I
    v, e, csp, home, alerts = env
    art = home / "Art"
    # her own files (ArtSafe knows these from her protected folders)
    (art / "2026 Photoshop").mkdir()
    I.new("RGB", (40, 30), "red").save(art / "2026 Photoshop" / "Silver_halloween2026.psd", "PNG")
    I.new("RGB", (40, 30), "blue").save(art / "Spyx_Edit2026.psd", "PNG")
    # Clip Studio's backups, named the way it names them
    doc, init = csp / "DocumentBackup", csp / "InitialBackup"
    doc.mkdir(); init.mkdir(); (csp / "RecoveryBackup").mkdir()
    I.new("RGB", (40, 30), "green").save(doc / "20261031153212Spyx_Edit2026.psd", "PNG")
    make_clip(doc / "20261025212158Silver_halloween2026.clip", "orange")
    I.new("RGB", (40, 30), "white").save(init / "2026 PhotoshopSilver_halloween2026.psd", "PNG")
    (csp / "RecoveryBackup" / "canvas.dat").write_bytes(b"recovery data")
    e.full_scan()

    known = frozenset(f.name for f in v.list_files(False))
    got = sorted((describe(s.path, s.app, known)) for s in v.list_autosaves())
    assert got == [
        ("Silver_halloween2026", "Clip Studio backup from a save"),
        ("Silver_halloween2026", "Clip Studio copy from when it was first opened"),
        ("Spyx_Edit2026", "Clip Studio backup from a save"),
        ("canvas", "Clip Studio recovery copy"),
    ]
