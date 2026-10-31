"""Her upgrade path: real 1.0.0 (backs up every PNG) -> self-update -> new version asks to clean up.

Expects:
  %TEMP%\\artsafe-upgrade\\Apps\\ArtSafe           1.0.0 from the GitHub release
  %TEMP%\\artsafe-upgrade\\server\\ArtSafe-X.zip(.sig)  the new version (release.ps1 -NoPublish)
"""
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from PIL import Image

T = Path(os.environ["TEMP"]) / "artsafe-upgrade"
APP, SERVER, PORT = T / "Apps" / "ArtSafe", T / "server", 8766
NEW = sys.argv[1]


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SERVER), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/releases/latest"):
            body = json.dumps({"tag_name": f"v{NEW}", "assets": [
                {"name": n, "browser_download_url": f"http://127.0.0.1:{PORT}/{n}"}
                for n in (f"ArtSafe-{NEW}.zip", f"ArtSafe-{NEW}.zip.sig")]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

home = T / "home"
art = home / "Art"
art.mkdir(parents=True)
from usertest_env import layered_psd
layered_psd(art / "commission.psd")
for i in range(40):  # lots of exports/references, like her folders
    Image.new("RGB", (800, 600), (i * 5, 120, 200)).save(art / f"export_{i:02}.png")
cfg_dir = home / "AppData" / "Roaming" / "ArtSafe"
cfg_dir.mkdir(parents=True)
old_default = [".psd", ".psb", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".ai", ".clip"]
(cfg_dir / "config.json").write_text(json.dumps({  # like the config 1.0.0 saved on her PC
    "watched_folders": [str(art)], "extensions": old_default, "first_run_done": True,
    "start_with_windows": False, "send_error_reports": False, "cloud_enabled": False}))
env = dict(os.environ, USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
           LOCALAPPDATA=str(home / "AppData" / "Local"), ARTSAFE_UPDATE_API=f"http://127.0.0.1:{PORT}",
           ARTSAFE_UPDATE_REPO_TEST="test/artsafe", ARTSAFE_UPDATE_DELAY="20")
vault = home / "AppData" / "Local" / "ArtSafe" / "Vault"
logf = home / "AppData" / "Local" / "ArtSafe" / "logs" / "artsafe.log"


def known():
    db = sqlite3.connect(vault / "index.db")
    names = sorted(Path(r[0]).name for r in db.execute("SELECT path FROM files"))
    db.close()
    return names


subprocess.Popen([str(APP / "ArtSafe.exe"), "--tray"], env=env)
time.sleep(15)
names = known()
print(f"1.0.0 backed up {len(names)} files ({sum(n.endswith('.png') for n in names)} PNGs), "
      f"backup folder {sum(f.stat().st_size for f in vault.rglob('*') if f.is_file()) / 1024**2:.1f} MB")

# wait for the update to install and the new version to start
deadline = time.time() + 120
while time.time() < deadline:
    time.sleep(2)
    if logf.exists() and f"ArtSafe {NEW} starting" in logf.read_text(encoding="utf-8", errors="replace"):
        break
print("new version started:", f"ArtSafe {NEW} starting" in logf.read_text(encoding="utf-8", errors="replace"))
time.sleep(6)

import ui
ui.EXE = APP / "ArtSafe.exe"
q = ui.message_box("Yes", timeout=30)
print("question shown:", " ".join(q.split("\n")[:3])[:260])
time.sleep(8)
from pywinauto import Application
for w in Application(backend="win32").connect(path=str(APP / "ArtSafe.exe")).windows():
    if w.is_visible():
        print("  visible window:", repr(w.window_text()), w.class_name())
print("  log tail:")
for line in logf.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]:
    print("   ", line[20:200])
try:
    print("then:", ui.message_box("OK", timeout=10).split("\n")[0])
except Exception as e:
    print("no OK box:", e)
names = known()
print(f"after cleanup: backups of {names}")
print(f"backup folder now {sum(f.stat().st_size for f in vault.rglob('*') if f.is_file()) / 1024**2:.1f} MB")
print("her PNGs still there:", len(list(art.glob("*.png"))))
cfg = json.loads((cfg_dir / "config.json").read_text())
print("config: include_images =", cfg["include_images"], "| pending_image_cleanup =", cfg["pending_image_cleanup"])
subprocess.run(["taskkill", "/F", "/IM", "ArtSafe.exe"], capture_output=True)
srv.shutdown()
