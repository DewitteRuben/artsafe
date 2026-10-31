"""Small JPEG previews so versions can be told apart at a glance."""
from __future__ import annotations

import os
from pathlib import Path
import logging

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

SIZE = 512
# Above this, only use the small preview stored inside the PSD. Rendering the full image
# could take gigabytes of memory and slow down her PC while she's working.
BIG_PSD = 150 * 1024 * 1024
MAX_PIXELS = 80_000_000  # same idea for huge flat images (e.g. a 9000 x 9000 PNG)


def _embedded_psd_thumbnail(src: Path) -> Image.Image | None:
    """Read only the small JPEG preview near the start of a PSD/PSB (resource 1036)."""
    import io
    import struct

    with open(src, "rb") as f:
        if f.read(4) != b"8BPS":
            return None
        f.seek(26)
        (color_len,) = struct.unpack(">I", f.read(4))
        f.seek(color_len, 1)
        (res_len,) = struct.unpack(">I", f.read(4))
        end = f.tell() + res_len
        while f.tell() + 12 <= end:
            if f.read(4) != b"8BIM":
                return None
            (rid,) = struct.unpack(">H", f.read(2))
            (name_len,) = struct.unpack(">B", f.read(1))
            f.seek(name_len + (1 - name_len % 2), 1)  # Pascal string padded to even length
            (size,) = struct.unpack(">I", f.read(4))
            if rid in (1036, 1033):
                data = f.read(size)
                img = Image.open(io.BytesIO(data[28:]))
                img.load()
                if rid == 1033:  # very old format stored BGR
                    r, g, b = img.split()[:3]
                    img = Image.merge("RGB", (b, g, r))
                return img
            f.seek(size + (size % 2), 1)
    return None


def _open_psd(src: Path) -> Image.Image | None:
    if src.stat().st_size >= BIG_PSD:
        try:
            return _embedded_psd_thumbnail(src)
        except Exception:
            return None

    from psd_tools import PSDImage

    psd = PSDImage.open(src)
    small = src.stat().st_size < BIG_PSD and psd.width * psd.height <= MAX_PIXELS
    if small:
        try:
            img = psd.topil()
            if img is not None:
                return img
        except Exception:
            pass
    try:
        img = psd.thumbnail()
        if img is not None:
            return img
    except Exception:
        pass
    # No ready-made preview inside (common for recovery files): build one from the layers.
    if small:
        try:
            return psd.composite()
        except Exception:
            pass
    return None


def _open_clip(src: Path) -> Image.Image | None:
    """Clip Studio files contain an SQLite database with a preview of the canvas."""
    import io
    import mmap
    import sqlite3
    import struct
    import tempfile

    with open(src, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        i = m.find(b"CHNKSQLi")
        if i >= 0:
            (length,) = struct.unpack(">Q", m[i + 8:i + 16])
            data = m[i + 16:i + 16 + length]
        else:
            j = m.find(b"SQLite format 3\x00")
            if j < 0:
                return None
            data = m[j:]
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as t:
            t.write(data)
        con = sqlite3.connect(tmp)
        try:
            row = con.execute("SELECT ImageData FROM CanvasPreview LIMIT 1").fetchone()
        finally:
            con.close()
    finally:
        os.unlink(tmp)
    if not row or not row[0]:
        return None
    img = Image.open(io.BytesIO(row[0]))
    img.load()
    return img


def _open_ai(src: Path) -> Image.Image | None:
    """Illustrator stores a JPEG preview (base64) in the file's XMP metadata."""
    import base64
    import io

    start, end = b"<xmpGImg:image>", b"</xmpGImg:image>"
    with open(src, "rb") as f:
        data = f.read(16 * 1024 * 1024)
        if start not in data and src.stat().st_size < 400 * 1024 * 1024:
            data += f.read()
    i = data.find(start)
    if i < 0:
        return None
    j = data.find(end, i)
    b64 = data[i + len(start):j].replace(b"&#xA;", b"").replace(b"\n", b"").replace(b"\r", b"")
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    img.load()
    return img


def make_thumbnail(src: Path, out: Path) -> bool:
    try:
        ext = src.suffix.lower()
        if ext in (".psd", ".psb"):
            img = _open_psd(src)
        elif ext == ".clip":
            img = _open_clip(src)
        elif ext == ".ai":
            img = _open_ai(src)
        else:
            img = Image.open(src)
            img.draft("RGB", (SIZE, SIZE))
            if img.width * img.height > MAX_PIXELS and img.format != "JPEG":
                return False
        if img is None:
            return False
        img.thumbnail((SIZE, SIZE))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "JPEG", quality=85)
        return True
    except Exception:
        logging.getLogger(__name__).warning("No preview for %s", src, exc_info=True)
        return False
