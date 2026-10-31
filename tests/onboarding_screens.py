"""Render each setup step into ./screenshots using a fake home folder."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
home = tmp / "Anna"
os.environ["USERPROFILE"] = str(home)
os.environ["APPDATA"] = str(tmp / "roaming")
os.environ["LOCALAPPDATA"] = str(tmp / "local")
os.environ["OneDrive"] = str(home / "OneDrive")
os.environ.pop("OneDriveConsumer", None)
os.environ.pop("OneDriveCommercial", None)

for rel, n in [("Desktop/Commissions", 14), ("Pictures/Fanart", 6), ("Documents/School", 2), ("OneDrive", 0)]:
    d = home / rel
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"piece{i}.psd").write_bytes(b"8BPS" + b"\0" * 300_000)

from PySide6.QtWidgets import QApplication

from artsafe import onboarding
from artsafe.config import Config
from artsafe.gui import app_style

onboarding.candidate_roots = lambda: [home / "Desktop", home / "Documents", home / "Pictures", home / "OneDrive"]
app = QApplication(sys.argv)
from artsafe.gui import use_light_theme
use_light_theme(app)
app.setStyleSheet(app_style())
cfg = Config.load()
w = onboarding.Onboarding(cfg)
w.show()
for _ in range(20):
    app.processEvents()
    time.sleep(0.05)
out = ROOT / "screenshots"
out.mkdir(exist_ok=True)
for i in range(5):
    w._go(i)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.03)
    w.grab().save(str(out / f"setup{i + 1}.png"))
w.startup.setChecked(False)  # don't touch this PC's real startup settings
import artsafe.gui
artsafe.gui.set_start_with_windows = lambda on: None
w._apply()
saved = Config.load()
print("folders:", [Path(f).name for f in saved.watched_folders], "cloud:", saved.cloud_enabled,
      "snapshots:", saved.ps_snapshots)
w.reject()
