"""Render each screen with sample data into ./screenshots (uses a throwaway profile)."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
os.environ["APPDATA"] = str(tmp / "roaming")
os.environ["LOCALAPPDATA"] = str(tmp / "local")

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.gui import app_style, Bridge, MainWindow, make_icon
from artsafe.vault import Vault

art = tmp / "Pictures" / "Art"
(art / "Commissions").mkdir(parents=True)
cfg = Config(watched_folders=[str(art)], cloud_enabled=True,
             cloud_dir=str(tmp / "OneDrive" / "ArtSafe Backup"), first_run_done=True,
             start_with_windows=False)
cfg.save()
vault = Vault(cfg)

t = time.time() - 3 * 86400


def paint(path, bg, shapes):
    im = Image.new("RGB", (800, 600), bg)
    d = ImageDraw.Draw(im)
    for i, color in enumerate(shapes):
        d.ellipse((80 + i * 160, 150 + (i % 2) * 120, 300 + i * 160, 370 + (i % 2) * 120), fill=color)
    im.save(path)
    global t
    t += 3700
    os.utime(path, (t, t))
    v = vault.backup_file(path)
    vault.db.execute("UPDATE versions SET backed_up_at=? WHERE id=?", (t, v.id))
    vault.db.commit()


f = art / "dragon_final.png"
paint(f, "#1d2b53", ["#ff004d"])
paint(f, "#1d2b53", ["#ff004d", "#ffa300"])
paint(f, "#1d2b53", ["#ff004d", "#ffa300", "#00e436"])
paint(f, "#1d2b53", ["#c2c3c7"])  # the "oops, merged everything" version
g = art / "Commissions" / "portrait_anna.png"
paint(g, "#fff1e8", ["#7e2553", "#29adff"])
paint(g, "#fff1e8", ["#7e2553", "#29adff", "#83769c"])
h = art / "sketch_cat.png"
paint(h, "#ffffff", ["#5f574f"])
os.remove(h)
vault.mark_deleted(h)

from psd_tools import PSDImage
rec = tmp / "roaming" / "Adobe" / "Adobe Photoshop 2026" / "AutoRecover"
rec.mkdir(parents=True)
for i, colors in enumerate([["#ff004d"], ["#ff004d", "#ffa300"], ["#ff004d", "#ffa300", "#00e436"]]):
    im = Image.new("RGB", (800, 600), "#1d2b53")
    d = ImageDraw.Draw(im)
    for j, c in enumerate(colors):
        d.ellipse((80 + j * 160, 150 + (j % 2) * 120, 300 + j * 160, 370 + (j % 2) * 120), fill=c)
    psb = rec / "dragon_final_8c1e.psb"
    PSDImage.frompil(im).save(psb)
    t += 300
    os.utime(psb, (t, t))
    v = vault.backup_file(psb, kind="autosave", app="Photoshop")
    vault.db.execute("UPDATE versions SET backed_up_at=? WHERE id=?", (t, v.id))
    vault.db.commit()

stage = Path(cfg.vault_dir) / "staging"
stage.mkdir(parents=True, exist_ok=True)
for colors in (["#29adff"], ["#29adff", "#ff77a8"]):
    im = Image.new("RGB", (800, 600), "#fff1e8")
    d = ImageDraw.Draw(im)
    for j, c in enumerate(colors):
        d.rectangle((100 + j * 250, 150, 300 + j * 250, 450), fill=c)
    psb = stage / "portrait_anna [3fa9c1].psb"
    PSDImage.frompil(im).save(psb)
    t += 300
    os.utime(psb, (t, t))
    v = vault.backup_file(psb, reason="snapshot", kind="autosave", app="Photoshop snapshot")
    vault.db.execute("UPDATE versions SET backed_up_at=? WHERE id=?", (t, v.id))
    vault.db.commit()
    psb.unlink()

sys.path.insert(0, str(ROOT / "tests"))
from test_clipstudio import make_clip
csp = tmp / "roaming" / "CELSYSUserData" / "CELSYS" / "CLIPStudioPaintData"
for name, color in (("DocumentBackup/comic_page3_20261031_141502.clip", "#ffa300"),
                    ("DocumentBackup/comic_page3_20261031_152210.clip", "#29adff"),
                    ("InitialBackup/comic_page3.clip", "#ffffff")):
    f = csp / name
    make_clip(f, color)
    t += 200
    os.utime(f, (t, t))
    v = vault.backup_file(f, kind="autosave", app="Clip Studio")
    vault.db.execute("UPDATE versions SET backed_up_at=? WHERE id=?", (t, v.id))
    vault.db.commit()

app = QApplication(sys.argv)
from artsafe.gui import use_light_theme
use_light_theme(app)
app.setStyleSheet(app_style())
engine = Engine(cfg, vault)
win = MainWindow(cfg, vault, engine, Bridge())
win._activity("Backed up dragon_final.png at 14:32")
win.show()
out = ROOT / "screenshots"
out.mkdir(exist_ok=True)
for i, name in enumerate(["files", "deleted", "unsaved", "settings"]):
    win.nav.setCurrentRow(i)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.05)
    win.grab().save(str(out / f"{name}.png"))
print("saved to", out)
vault.close()
