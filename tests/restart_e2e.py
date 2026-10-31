r"""Window open -> update downloads -> countdown -> restarts by itself -> window open again.

Expects %TEMP%\artsafe-restart\Apps\ArtSafe (installed) and server\ArtSafe-<NEW>.zip(.sig).
Pass "busy" to keep a dialog open when the countdown ends: it must wait for it.
"""
import http.server, json, os, subprocess, sys, threading, time
from pathlib import Path

T = Path(os.environ["TEMP"]) / "artsafe-restart"
APP, SERVER, PORT, NEW = T / "Apps" / "ArtSafe", T / "server", 8767, sys.argv[1]
busy = "busy" in sys.argv


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
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        else:
            super().do_GET()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
home = T / ("home-busy" if busy else "home")
(home / "Art").mkdir(parents=True, exist_ok=True)
cfg_dir = home / "AppData" / "Roaming" / "ArtSafe"
cfg_dir.mkdir(parents=True, exist_ok=True)
(cfg_dir / "config.json").write_text(json.dumps({
    "watched_folders": [str(home / "Art")], "first_run_done": True,
    "start_with_windows": False, "send_error_reports": False}))
env = dict(os.environ, USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
           LOCALAPPDATA=str(home / "AppData" / "Local"), ARTSAFE_UPDATE_API=f"http://127.0.0.1:{PORT}",
           ARTSAFE_UPDATE_REPO_TEST="test/artsafe", ARTSAFE_UPDATE_DELAY="5")
logf = home / "AppData" / "Local" / "ArtSafe" / "logs" / "artsafe.log"
read = lambda: logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""

from pywinauto import Desktop
subprocess.Popen([str(APP / "ArtSafe.exe")] + (["--tray"] if "tray" in sys.argv else []), env=env)  # window open, like she's using it
t0 = time.time()


from pywinauto import Desktop as D
from PIL import ImageGrab


def find_countdown():
    for w in D(backend="uia").windows():
        try:
            if w.window_text() != "ArtSafe":
                continue
            texts = [t.window_text() for t in w.descendants(control_type="Text")]
            if any("restart in" in x for x in texts):
                return w, " ".join(texts)
        except Exception:
            pass


box = None
while time.time() - t0 < 120 and box is None:
    time.sleep(1)
    box = find_countdown()
print(f"countdown shown after {time.time() - t0:.0f}s:", box is not None)
if box:
    print("  text:", " ".join(box[1].split())[:220])
    time.sleep(2)
    r = box[0].rectangle()
    ImageGrab.grab((r.left, r.top, r.right, r.bottom)).save(T / "countdown.png")
if busy:  # she opens Settings > Add folder (a file picker) and leaves it open past the countdown
    from pywinauto import Application; main = Application(backend="uia").connect(handle=box[0].handle).window(handle=box[0].handle)
    main.set_focus()
    main.child_window(title_re=".*Settings", control_type="ListItem").click_input()
    time.sleep(1)
    main.child_window(title_re="Add folder.*", control_type="Button").invoke()
    time.sleep(75)
    print("still waiting while the picker is open:", f"ArtSafe {NEW} starting" not in read())
    from pywinauto.keyboard import send_keys
    for w in D(backend="win32").windows(class_name="#32770"):
        w.close()
    t_close = time.time()
deadline = time.time() + 120
while time.time() < deadline and f"ArtSafe {NEW} starting" not in read():
    time.sleep(2)
print(f"restarted into {NEW} after {time.time() - t0:.0f}s:", f"ArtSafe {NEW} starting" in read())
time.sleep(6)
print("window open again:", any(w.window_text() == "ArtSafe" for w in D(backend="uia").windows()))
if busy:
    print(f"restarted {time.time() - t_close - 6:.0f}s after the picker closed (approx)")
for line in read().splitlines():
    if any(k in line for k in ("update", "Update", "starting")):
        print("   ", line[20:170])
subprocess.run(["taskkill", "/F", "/IM", "ArtSafe.exe"], capture_output=True)
srv.shutdown()
