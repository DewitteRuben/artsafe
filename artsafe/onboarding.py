"""First-run setup: a few friendly steps instead of a settings page."""
from __future__ import annotations

import os
import string
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .config import APP_NAME, REQUIRED_EXTENSIONS, Config, detect_cloud_folders

ART_EXTENSIONS = set(REQUIRED_EXTENSIONS) | {".kra", ".procreate", ".sai", ".sai2", ".xcf", ".afphoto"}
SKIP_DIRS = {"appdata", "windows", "program files", "program files (x86)", "programdata",
             "$recycle.bin", "system volume information", "node_modules", ".git", "artsafe backup"}
SCAN_SECONDS = 6
MAX_DEPTH = 5

WIZARD_STYLE = """
QDialog { background: #f6f5fb; }
#h1 { font-size: 20pt; font-weight: 600; }
#lead { font-size: 11pt; color: #4b4a5a; }
#feature { background: #ffffff; border: 1px solid #e6e4f0; border-radius: 12px; padding: 12px; }
#featureTitle { font-weight: 600; font-size: 11pt; }
#steps { background: #ffffff; border: 1px solid #e6e4f0; border-radius: 12px; padding: 14px; }
#dot { background: #dcd8ec; border-radius: 4px; }
#dotOn { background: #6C4CF1; border-radius: 4px; }
QRadioButton { padding: 10px 12px; background: #ffffff; border: 1px solid #e6e4f0; border-radius: 10px; font-size: 10.5pt; }
QRadioButton:checked { border: 2px solid #6C4CF1; background: #f4f1ff; }
QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px; border: 2px solid #9b98ad; background: #ffffff; }
QRadioButton::indicator:checked { border: 2px solid #6C4CF1; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 #6C4CF1, stop:0.55 #6C4CF1, stop:0.6 #ffffff, stop:1 #ffffff); }
QListWidget::item { padding: 8px; }
"""


def pretty_path(p: str | Path) -> str:
    """C:\\Users\\Anna\\Desktop\\Commissions -> Desktop › Commissions"""
    p = Path(p)
    try:
        rel = p.relative_to(Path.home())
        return " › ".join(rel.parts) or "Your user folder"
    except ValueError:
        return str(p)


# ---------- finding her art ----------
def candidate_roots() -> list[Path]:
    home = Path.home()
    roots = [home / "Desktop", home / "Documents", home / "Pictures", home / "Downloads"]
    roots += list(detect_cloud_folders().values())
    for letter in string.ascii_uppercase[3:]:  # other drives, e.g. D:\
        d = Path(f"{letter}:\\")
        try:
            if d.exists():
                roots.append(d)
        except OSError:
            pass
    seen, out = set(), []
    for r in roots:
        try:
            key = os.path.normcase(str(r.resolve()))
        except OSError:
            continue
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def suggest_folder(file: Path, root: Path) -> Path:
    """Suggest the top folder under a root (e.g. Desktop\\Commissions), or the root itself."""
    rel = file.relative_to(root)
    if len(rel.parts) <= 1:
        return root
    # whole drives are too broad: suggest the folder on the drive instead
    return root / rel.parts[0]


class ArtFinder(QObject):
    """Looks around common places for art files, without taking more than a few seconds."""
    found = Signal(list)  # [(folder, file_count, total_bytes)]

    def __init__(self):
        super().__init__()
        self.abort = False

    def run(self) -> None:
        deadline = time.time() + SCAN_SECONDS
        stats: dict[str, list] = {}
        for root in candidate_roots():
            base_depth = len(root.parts)
            for dirpath, dirs, files in os.walk(root):
                if time.time() > deadline or self.abort:
                    break
                depth = len(Path(dirpath).parts) - base_depth
                dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")
                           and depth < MAX_DEPTH]
                for name in files:
                    if Path(name).suffix.lower() in ART_EXTENSIONS:
                        f = Path(dirpath) / name
                        folder = suggest_folder(f, root)
                        try:
                            size = f.stat().st_size
                        except OSError:
                            size = 0
                        entry = stats.setdefault(str(folder), [0, 0])
                        entry[0] += 1
                        entry[1] += size
        result = sorted(((k, v[0], v[1]) for k, v in stats.items()), key=lambda x: -x[1])[:10]
        self.found.emit(result)


# ---------- wizard ----------
class Onboarding(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        from .gui import fmt_size, make_icon  # local import: gui imports this module

        self._fmt_size = fmt_size
        self.cfg = cfg
        self.setWindowTitle(f"Set up {APP_NAME}")
        self.setWindowIcon(make_icon())
        self.setFixedSize(780, 600)
        self.setStyleSheet(WIZARD_STYLE)
        self.art_bytes = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 24)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        self.dots = []
        for _ in range(5):
            d = QFrame()
            d.setFixedSize(22, 8)
            self.dots.append(d)
            bar.addWidget(d)
        bar.addStretch(1)
        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next = QPushButton("Next")
        self.next.setObjectName("primary")
        self.next.setMinimumWidth(130)
        self.next.clicked.connect(self._next)
        bar.addWidget(self.back)
        bar.addWidget(self.next)
        outer.addLayout(bar)

        self.stack.addWidget(self._welcome(make_icon))
        self.stack.addWidget(self._folders())
        self.stack.addWidget(self._cloud())
        self.stack.addWidget(self._photoshop())
        self.stack.addWidget(self._done())
        self._go(0)
        self._start_search()

    # ----- helpers -----
    @staticmethod
    def _title(text: str, lead: str) -> tuple[QLabel, QLabel]:
        t = QLabel(text)
        t.setObjectName("h1")
        t.setWordWrap(True)
        l = QLabel(lead)
        l.setObjectName("lead")
        l.setWordWrap(True)
        return t, l

    def _page(self, title: str, lead: str) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        t, l = self._title(title, lead)
        v.addWidget(t)
        v.addWidget(l)
        v.addSpacing(8)
        return w, v

    # ----- pages -----
    def _welcome(self, make_icon) -> QWidget:
        w, v = self._page("Hi! Let's keep your art safe 💜",
                          "ArtSafe quietly keeps copies of your work in the background, so a wrong "
                          "click can never cost you hours of drawing again. Setup takes about a minute.")
        for icon, title, body in (
            ("💾", "Every save is kept",
             "Saved over something by accident, or merged layers? Go back to any earlier version."),
            ("🗑️", "Deleted files can come back",
             "If a file gets deleted, ArtSafe still has it. Put it back with one click."),
            ("📸", "Even unsaved work",
             "Every few minutes ArtSafe takes a snapshot of what's open in Photoshop, even if you haven't saved."),
        ):
            card = QFrame()
            card.setObjectName("feature")
            h = QHBoxLayout(card)
            ic = QLabel(icon)
            ic.setStyleSheet("font-size: 22pt; padding-right: 8px;")
            txt = QVBoxLayout()
            a = QLabel(title)
            a.setObjectName("featureTitle")
            b = QLabel(body)
            b.setObjectName("muted")
            b.setWordWrap(True)
            txt.addWidget(a)
            txt.addWidget(b)
            h.addWidget(ic)
            h.addLayout(txt, 1)
            v.addWidget(card)
        v.addStretch(1)
        return w

    def _folders(self) -> QWidget:
        w, v = self._page("Where do you keep your art?",
                          "We're looking around your PC for Photoshop, Clip Studio and Illustrator files. "
                          "Tick the folders to protect. Everything inside them, including subfolders, is "
                          "backed up. You can change this any time.")
        self.folder_list = QListWidget()
        self.folder_list.setIconSize(QSize(20, 20))
        self.folder_list.itemChanged.connect(self._update_buttons)
        self.searching = QLabel("🔎  Looking for your art…")
        self.searching.setObjectName("muted")
        add = QPushButton("+  Add another folder…")
        add.clicked.connect(self._add_folder)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self.searching)
        v.addWidget(self.folder_list, 1)
        v.addLayout(row)
        for f in self.cfg.watched_folders:
            self._add_folder_item(f, checked=True)
        return w

    def _cloud(self) -> QWidget:
        w, v = self._page("Keep a copy in the cloud too?",
                          "Backups on this PC protect you from mistakes. A cloud copy also protects you "
                          "if the computer breaks or gets stolen. The newest version of each file goes to "
                          "a folder that your cloud app uploads.")
        self.cloud_group = QButtonGroup(self)
        self.cloud_choices: dict[QRadioButton, str | None] = {}
        clouds = detect_cloud_folders()
        first = True
        for name, path in clouds.items():
            label = f"☁️  {name}" + ("  (recommended)" if first else "")
            rb = QRadioButton(f"{label}\n      {pretty_path(path)}")
            self.cloud_choices[rb] = str(Path(path) / "ArtSafe Backup")
            first = False
        other = self.other_rb = QRadioButton("📁  Another folder…")
        self.cloud_choices[other] = "pick"
        none = QRadioButton("🚫  No thanks, only keep backups on this PC")
        self.cloud_choices[none] = None
        for rb in self.cloud_choices:
            self.cloud_group.addButton(rb)
            v.addWidget(rb)
        current = self.cfg.cloud_dir if self.cfg.cloud_enabled else None
        chosen = next((rb for rb, p in self.cloud_choices.items() if p and p == current), None)
        if chosen is None and current:
            other.setText(f"📁  Another folder…\n      {pretty_path(current)}")
            self.cloud_choices[other] = current
            chosen = other
        if chosen is None:
            chosen = next(iter(self.cloud_choices)) if clouds else none
        chosen.setChecked(True)
        other.clicked.connect(self._pick_cloud)
        self.space_note = QLabel()
        self.space_note.setObjectName("muted")
        self.space_note.setWordWrap(True)
        v.addSpacing(6)
        v.addWidget(self.space_note)
        if not clouds:
            hint = QLabel("No cloud app (OneDrive, Google Drive, Dropbox) was found on this PC. "
                          "You can install one later and turn this on in Settings.")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            v.addWidget(hint)
        v.addStretch(1)
        return w

    def _photoshop(self) -> QWidget:
        w, v = self._page("Protect work you haven't saved yet",
                          "These keep extra copies while you're working, so even unsaved progress "
                          "can be brought back, in Photoshop and Clip Studio.")
        self.snap = QCheckBox("Take a snapshot of open Photoshop files every 5 minutes (recommended)")
        self.snap.setChecked(self.cfg.ps_snapshots)
        snap_note = QLabel("Only when something changed, and only during a short pause in your drawing. "
                           "Your real files are never touched.")
        snap_note.setObjectName("muted")
        snap_note.setWordWrap(True)
        snap_note.setContentsMargins(26, 0, 0, 0)
        self.autos = QCheckBox("Also keep the backups Photoshop and Clip Studio make themselves (recommended)")
        self.autos.setChecked(self.cfg.capture_autosaves)
        self.startup = QCheckBox("Start ArtSafe automatically with Windows (recommended)")
        self.startup.setChecked(self.cfg.start_with_windows)
        v.addWidget(self.snap)
        v.addWidget(snap_note)
        v.addSpacing(4)
        v.addWidget(self.autos)
        v.addWidget(self.startup)
        v.addSpacing(10)
        steps = QFrame()
        steps.setObjectName("steps")
        sv = QVBoxLayout(steps)
        head = QLabel("✨  Two quick settings in your drawing apps")
        head.setObjectName("featureTitle")
        body = QLabel(
            "<b>Photoshop:</b>  Edit → Preferences → File Handling → tick "
            "“Automatically Save Recovery Information Every” and choose <b>5 Minutes</b>.<br><br>"
            "<b>Clip Studio Paint:</b>  File → Preferences → File → Auto-Recovery → tick "
            "“Enable canvas recovery” and pick a short interval."
        )
        body.setWordWrap(True)
        body.setStyleSheet("padding-top: 4px;")
        sv.addWidget(head)
        sv.addWidget(body)
        v.addWidget(steps)
        v.addStretch(1)
        return w

    def _done(self) -> QWidget:
        w, v = self._page("You're all set! 🎉", "")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 11pt; line-height: 160%;")
        tray = QFrame()
        tray.setObjectName("steps")
        tv = QVBoxLayout(tray)
        t = QLabel("🛡️  Where to find ArtSafe")
        t.setObjectName("featureTitle")
        b = QLabel("ArtSafe runs in the background. Its shield icon sits near the clock in the bottom-right "
                   "corner (click the little ^ arrow if you don't see it). Click it any time to get "
                   "an older version back.")
        b.setWordWrap(True)
        tv.addWidget(t)
        tv.addWidget(b)
        v.addWidget(self.summary)
        v.addSpacing(8)
        v.addWidget(tray)
        v.addStretch(1)
        return w

    # ----- behaviour -----
    def _start_search(self) -> None:
        self._thread = QThread(self)
        self._finder = ArtFinder()
        self._finder.moveToThread(self._thread)
        self._thread.started.connect(self._finder.run)
        self._finder.found.connect(self._search_done)
        self._finder.found.connect(self._thread.quit)
        self._thread.start()

    def _search_done(self, results: list) -> None:
        existing = {os.path.normcase(i.data(Qt.UserRole)) for i in self._items()}
        nothing_chosen_yet = not self.cfg.watched_folders
        for folder, count, size in results:
            self.art_bytes += size
            if os.path.normcase(folder) in existing:
                continue
            self._add_folder_item(folder, checked=nothing_chosen_yet, count=count)
        if results:
            self.searching.setText(f"Found art in {len(results)} place{'s' if len(results) != 1 else ''}")
        else:
            self.searching.setText("No art files found automatically. Add your folders by hand.")
        self._update_buttons()

    def _items(self) -> list[QListWidgetItem]:
        return [self.folder_list.item(i) for i in range(self.folder_list.count())]

    def _add_folder_item(self, folder: str, checked: bool, count: int | None = None) -> None:
        text = f"📁  {pretty_path(folder)}"
        if count:
            text += f"    ·  {count} art file{'s' if count != 1 else ''}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, folder)
        item.setToolTip(folder)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.folder_list.addItem(item)

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a folder with your art", str(Path.home()))
        if not d:
            return
        d = os.path.normpath(d)
        for item in self._items():
            if os.path.normcase(item.data(Qt.UserRole)) == os.path.normcase(d):
                item.setCheckState(Qt.Checked)
                return
        self._add_folder_item(d, checked=True)
        self._update_buttons()

    def chosen_folders(self) -> list[str]:
        return [i.data(Qt.UserRole) for i in self._items() if i.checkState() == Qt.Checked]

    def _pick_cloud(self) -> None:
        clouds = detect_cloud_folders()
        start = str(next(iter(clouds.values()), Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Choose a folder that your cloud app syncs", start)
        other = self.other_rb
        if d:
            d = os.path.normpath(d)
            if Path(d).name != "ArtSafe Backup":
                d = os.path.join(d, "ArtSafe Backup")
            self.cloud_choices[other] = d
            other.setText(f"📁  Another folder…\n      {pretty_path(d)}")
        elif self.cloud_choices[other] == "pick":
            # cancelled without a folder: fall back to the first option
            next(iter(self.cloud_choices)).setChecked(True)

    def chosen_cloud(self) -> str | None:
        rb = self.cloud_group.checkedButton()
        p = self.cloud_choices.get(rb)
        return None if p in (None, "pick") else p

    def _go(self, i: int) -> None:
        i = max(0, min(i, self.stack.count() - 1))
        self.stack.setCurrentIndex(i)
        for n, d in enumerate(self.dots):
            d.setObjectName("dotOn" if n <= i else "dot")
            d.style().unpolish(d)
            d.style().polish(d)
        self.back.setVisible(0 < i < self.stack.count() - 1)
        self.next.setText({0: "Let's go", self.stack.count() - 1: "Finish"}.get(i, "Next"))
        if i == 2:
            gb = self.art_bytes / 1024**3
            self.space_note.setText(
                f"Your art files take about {self._fmt_size(self.art_bytes)}. Free OneDrive accounts "
                "have 5 GB, so you may need more storage there." if gb > 4 else
                (f"Your art files take about {self._fmt_size(self.art_bytes)}." if self.art_bytes else ""))
        if i == self.stack.count() - 1:
            self._fill_summary()
        self._update_buttons()

    def _update_buttons(self, *_) -> None:
        if self.stack.currentIndex() == 1:
            ok = bool(self.chosen_folders())
            self.next.setEnabled(ok)
            self.next.setToolTip("" if ok else "Tick at least one folder")
        else:
            self.next.setEnabled(True)

    def _fill_summary(self) -> None:
        folders = self.chosen_folders()
        cloud = self.chosen_cloud()
        lines = [f"✅  Protecting {len(folders)} folder{'s' if len(folders) != 1 else ''}"]
        lines.append(f"✅  Cloud copy in {pretty_path(cloud)}" if cloud else "➖  No cloud copy (you can turn it on later)")
        lines.append("✅  Photoshop snapshots every 5 minutes" if self.snap.isChecked()
                     else "➖  No Photoshop snapshots")
        if self.startup.isChecked():
            lines.append("✅  Starts automatically with Windows")
        lines.append("\nArtSafe is now backing up your files for the first time. "
                     "You can keep working while it does.")
        self.summary.setText("\n".join(lines))

    def _next(self) -> None:
        i = self.stack.currentIndex()
        if i < self.stack.count() - 1:
            self._go(i + 1)
        else:
            self._apply()
            self.accept()

    def _apply(self) -> None:
        from .gui import set_start_with_windows

        c = self.cfg
        c.watched_folders = self.chosen_folders()
        cloud = self.chosen_cloud()
        c.cloud_enabled = bool(cloud)
        if cloud:
            c.cloud_dir = cloud
        c.ps_snapshots = self.snap.isChecked()
        c.capture_autosaves = self.autos.isChecked()
        c.start_with_windows = self.startup.isChecked()
        c.first_run_done = True
        c.save()
        try:
            set_start_with_windows(c.start_with_windows)
        except OSError:
            pass

    def done(self, result: int) -> None:
        # stop the art search before the window goes away (accept, cancel, Esc or X)
        self._finder.abort = True
        self._thread.quit()
        self._thread.wait(SCAN_SECONDS * 1000 + 2000)
        super().done(result)
