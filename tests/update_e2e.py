"""End-to-end update test: an installed 1.0.0 updates itself from a local fake GitHub.

Expects (made by release.ps1 -NoPublish):
  %TEMP%\\artsafe-updatetest\\Apps\\ArtSafe          the "installed" 1.0.0
  %TEMP%\\artsafe-updatetest\\server\\ArtSafe-1.0.1.zip(.sig)
Optional arg "bad": serve a tampered zip instead, which must be refused.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

T = Path(os.environ["TEMP"]) / "artsafe-updatetest"
APP = T / "Apps" / "ArtSafe"
SERVER = T / "server"
PORT = 8765
bad = len(sys.argv) > 1 and sys.argv[1] == "bad"

if bad:  # tamper with the zip after it was signed
    with zipfile.ZipFile(SERVER / "ArtSafe-1.0.1.zip", "a") as z:
        z.writestr("ArtSafe/evil.txt", "not signed")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SERVER), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/repos/test/artsafe/releases/latest":
            body = json.dumps({"tag_name": "v1.0.1", "body": "test", "assets": [
                {"name": n, "browser_download_url": f"http://127.0.0.1:{PORT}/{n}"}
                for n in ("ArtSafe-1.0.1.zip", "ArtSafe-1.0.1.zip.sig")]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

home = T / "home"
cfg_dir = home / "AppData" / "Roaming" / "ArtSafe"
cfg_dir.mkdir(parents=True, exist_ok=True)
(home / "Art").mkdir(exist_ok=True)
(cfg_dir / "config.json").write_text(json.dumps({
    "watched_folders": [str(home / "Art")], "first_run_done": True,
    "start_with_windows": False, "send_error_reports": False}))
env = dict(os.environ, USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
           LOCALAPPDATA=str(home / "AppData" / "Local"), ARTSAFE_UPDATE_API=f"http://127.0.0.1:{PORT}",
           ARTSAFE_UPDATE_REPO_TEST="test/artsafe", ARTSAFE_UPDATE_DELAY="5")
logs = home / "AppData" / "Local" / "ArtSafe" / "logs"

p = subprocess.Popen([str(APP / "ArtSafe.exe"), "--tray"], env=env)
print("started 1.0.0, pid", p.pid)
deadline = time.time() + 120
started_versions = []
while time.time() < deadline:
    time.sleep(2)
    text = (logs / "artsafe.log").read_text(encoding="utf-8", errors="replace") if (logs / "artsafe.log").exists() else ""
    started_versions = [l.split("ArtSafe ")[1].split(" ")[0] for l in text.splitlines() if " starting · " in l]
    if "1.0.1" in started_versions or (bad and "signature is not valid" in text):
        break
time.sleep(4)
text = (logs / "artsafe.log").read_text(encoding="utf-8", errors="replace")
print("versions started:", started_versions)
for line in text.splitlines():
    if any(w in line for w in ("Update", "update", "signature")):
        print("  log:", line.split(": ", 1)[-1][:140])
if (logs / "update.log").exists():
    print("  update.log:", (logs / "update.log").read_text().strip())
print("old folder cleaned up:", not Path(str(APP) + ".old").exists())
print("app folder has ArtSafe.exe:", (APP / "ArtSafe.exe").exists())
print("unsigned file present:", (APP / "_internal").exists() and (APP / "evil.txt").exists())
subprocess.run(["taskkill", "/F", "/IM", "ArtSafe.exe"], capture_output=True)
srv.shutdown()
