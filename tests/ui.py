"""Small helpers for clicking around the real ArtSafe.exe with UI Automation."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

from usertest_env import BASE, EXE

SHOTS = BASE / "shots"


def app():
    return Application(backend="uia").connect(path=str(EXE), timeout=20)


def win(title, timeout=20):
    w = app().window(title=title)
    w.wait("visible", timeout=timeout)
    return w


def controls(w):
    return [(c.element_info.control_type, c.window_text()) for c in w.descendants()]


def show(w):
    for t, txt in controls(w):
        if txt:
            print(f"   {t:10} | {txt[:100]!r}")


def click(w, title, control_type="Button"):
    w.set_focus()
    w.child_window(title=title, control_type=control_type).wrapper_object().click_input()
    time.sleep(0.6)


def shot(w, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    w.set_focus()
    time.sleep(0.4)
    w.capture_as_image().save(SHOTS / f"{name}.png")


def message_box(button="Yes", timeout=10):
    """Find the newest ArtSafe message box, return its text and press a button."""
    end = time.time() + timeout
    while time.time() < end:
        for w in app().windows():
            if w.window_text() in ("ArtSafe", "Welcome to ArtSafe"):
                texts = [c.window_text() for c in w.descendants(control_type="Text")]
                btn = [b for b in w.descendants(control_type="Button") if b.window_text() == button]
                if btn:
                    msg = "\n".join(t for t in texts if t)
                    btn[0].click_input()
                    time.sleep(0.8)
                    return msg
        time.sleep(0.3)
    raise TimeoutError(f"no message box with a {button!r} button")


def _dialogs():
    a = Application(backend="win32").connect(path=str(EXE))
    return [w for w in a.windows(class_name="#32770") if w.is_visible()]


def file_dialog(path, timeout=15):
    """Type a path into a Windows save/open/folder dialog and confirm."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            dlgs = _dialogs()
        except Exception:
            dlgs = []
        if dlgs:
          try:
            hwnd = dlgs[0].handle
            title = dlgs[0].window_text()
            d = Application(backend="uia").connect(handle=hwnd).window(handle=hwnd)
            d.set_focus()
            for label in ("File name:", "Folder:"):
                edit = d.child_window(title=label, control_type="Edit")
                if edit.exists(timeout=1):
                    edit.wrapper_object().set_edit_text(str(path))
                    break
            send_keys("{ENTER}")
            time.sleep(1.2)
            if d.exists():
                btn = d.child_window(title="Select Folder", control_type="Button")
                if btn.exists(timeout=1):
                    btn.wrapper_object().click_input()
                    time.sleep(1)
            return title
          except Exception as e:  # dialog still starting up
            print("   (dialog not ready yet:", type(e).__name__, ")")
        time.sleep(0.3)
    raise TimeoutError("no file dialog")
