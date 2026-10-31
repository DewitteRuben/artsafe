import base64
import io
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from artsafe.config import Config
from artsafe.thumbs import make_thumbnail


def png_bytes(color, fmt="PNG"):
    b = io.BytesIO()
    Image.new("RGB", (120, 80), color).save(b, fmt)
    return b.getvalue()


def test_required_types_always_watched(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = Config(watched_folders=[str(tmp_path / "art")], extensions=[".png"])
    cfg.save()
    cfg = Config.load()
    for ext in (".psd", ".psb", ".clip", ".ai", ".PSD"):
        assert cfg.wants(tmp_path / "art" / f"x{ext}")
    assert not cfg.wants(tmp_path / "art" / "notes.txt")
    assert not cfg.wants(tmp_path / "elsewhere" / "x.psd")


def test_clip_thumbnail(tmp_path):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE CanvasPreview(ImageData BLOB)")
    con.execute("INSERT INTO CanvasPreview VALUES(?)", (png_bytes("orange"),))
    con.commit()
    con.close()
    data = db.read_bytes()
    clip = tmp_path / "art.clip"
    clip.write_bytes(b"CSFCHUNK" + b"\0" * 32 + b"CHNKSQLi" + struct.pack(">Q", len(data)) + data + b"CHNKFoot")
    out = tmp_path / "t.jpg"
    assert make_thumbnail(clip, out)
    r, g, b = Image.open(out).getpixel((10, 10))
    assert r > 240 and 150 < g < 180 and b < 20


def test_ai_thumbnail(tmp_path):
    b64 = base64.b64encode(png_bytes("green", "JPEG")).decode()
    wrapped = "&#xA;".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    ai = tmp_path / "logo.ai"
    ai.write_bytes(b"%PDF-1.6\n<x:xmpmeta><xmpGImg:image>" + wrapped.encode() + b"</xmpGImg:image></x:xmpmeta>")
    out = tmp_path / "t.jpg"
    assert make_thumbnail(ai, out)
    assert Image.open(out).size == (120, 80)
