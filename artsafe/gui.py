"""The window and tray icon."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFileIconProvider, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from . import __version__, logs, updater
import logging
log = logging.getLogger(__name__)
from .config import APP_NAME, Config, detect_cloud_folders
from .photoshop import APP_LABEL as SNAPSHOT_APP, Snapshotter, display_name
from .recovery import describe, find_sources
from .engine import Engine
from .vault import DiskFull, FileRow, Vault, VersionRow

ACCENT = "#6C4CF1"

STYLE = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: #1f1f29; }}
QMainWindow, #page {{ background: #f6f5fb; }}
QDialog, QMessageBox {{ background: #ffffff; }}
QMessageBox QLabel {{ color: #1f1f29; }}
QMenu {{ background: #ffffff; border: 1px solid #e6e4f0; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: #ece8ff; color: #1f1f29; }}
QMenu::separator {{ height: 1px; background: #e6e4f0; margin: 4px 8px; }}
QToolTip {{ background: #1f1f29; color: #ffffff; border: none; padding: 5px 8px; }}
#sidebar {{ background: #ffffff; border-right: 1px solid #e6e4f0; }}
#sidebar QListWidget {{ border: none; background: transparent; font-size: 11pt; }}
#sidebar QListWidget::item {{ padding: 10px 14px; border-radius: 8px; margin: 2px 8px; }}
#sidebar QListWidget::item:selected {{ background: #ece8ff; color: {ACCENT}; }}
#sidebar QListWidget::item:hover:!selected {{ background: #f4f2fb; }}
#brand {{ font-size: 15pt; font-weight: 600; padding: 18px 18px 12px 18px; }}
#status {{ padding: 10px 14px; border-radius: 10px; font-weight: 600; }}
#card {{ background: #ffffff; border: 1px solid #e6e4f0; border-radius: 12px; }}
#title {{ font-size: 14pt; font-weight: 600; }}
#muted {{ color: #6b6a7a; }}
#empty {{ color: #6b6a7a; font-size: 11pt; padding: 40px; }}
QListWidget {{ background: #ffffff; border: 1px solid #e6e4f0; border-radius: 10px; padding: 4px; outline: 0; }}
QListWidget::item {{ padding: 6px; border-radius: 8px; }}
QListWidget::item:selected {{ background: #ece8ff; color: #1f1f29; }}
QListWidget::item:hover:!selected {{ background: #f6f4fd; }}
QLineEdit, QSpinBox, QDoubleSpinBox {{ background: #ffffff; border: 1px solid #d9d6e6; border-radius: 8px; padding: 6px 8px; }}
QPushButton {{ background: #ffffff; border: 1px solid #d9d6e6; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ background: #f4f2fb; }}
QPushButton:disabled {{ color: #a9a8b5; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #5a3be0; }}
QPushButton#primary:disabled {{ background: #c9bff9; }}
QGroupBox {{ background: #ffffff; border: 1px solid #e6e4f0; border-radius: 12px; margin-top: 14px; padding: 16px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 4px; }}
QGroupBox * {{ font-weight: normal; }}
QSplitter::handle {{ background: transparent; width: 10px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1.5px solid #9b98ad; border-radius: 5px; background: #ffffff; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QScrollArea {{ border: none; background: transparent; }}
"""


# ---------- helpers ----------
def checkmark_file() -> str:
    """A white tick image for checkboxes (Qt stylesheets need it as a file)."""
    import tempfile

    path = Path(tempfile.gettempdir()) / "artsafe-check.png"
    if not path.exists():
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("white"), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline([_pt(8, 19), _pt(15, 26), _pt(28, 11)])
        p.end()
        pm.save(str(path))
    return str(path).replace("\\", "/")


def use_light_theme(app: QApplication) -> None:
    """ArtSafe is designed light. Without this, Windows dark mode gives dialogs a dark
    background while our text stays dark, which makes it unreadable."""
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except AttributeError:  # older Qt
        pass
    from PySide6.QtGui import QPalette

    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.Window, "#f6f5fb"), (QPalette.WindowText, "#1f1f29"), (QPalette.Base, "#ffffff"),
        (QPalette.AlternateBase, "#f6f5fb"), (QPalette.Text, "#1f1f29"), (QPalette.Button, "#ffffff"),
        (QPalette.ButtonText, "#1f1f29"), (QPalette.ToolTipBase, "#1f1f29"), (QPalette.ToolTipText, "#ffffff"),
        (QPalette.Highlight, "#ece8ff"), (QPalette.HighlightedText, "#1f1f29"),
        (QPalette.PlaceholderText, "#8a8899"),
    ):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)


def app_style() -> str:
    check = checkmark_file()
    return STYLE + f"""
QCheckBox::indicator:checked {{ image: url({check}); }}
QListView::indicator {{ width: 18px; height: 18px; border: 1.5px solid #9b98ad; border-radius: 5px; background: #ffffff; }}
QListView::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}
"""



def make_icon(size: int = 64, color: str = ACCENT) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64
    path = QPainterPath()
    path.moveTo(32 * s, 4 * s)
    path.lineTo(56 * s, 13 * s)
    path.cubicTo(56 * s, 38 * s, 46 * s, 52 * s, 32 * s, 60 * s)
    path.cubicTo(18 * s, 52 * s, 8 * s, 38 * s, 8 * s, 13 * s)
    path.closeSubpath()
    p.fillPath(path, QColor(color))
    pen = QPen(QColor("white"), 6 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([pt for pt in (
        _pt(21 * s, 32 * s), _pt(29 * s, 40 * s), _pt(43 * s, 24 * s))])
    p.end()
    return QIcon(pm)


def _pt(x, y):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)


def fmt_time(ts: float, short: bool = False) -> str:
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    delta = (today - d.date()).days
    clock = d.strftime("%H:%M")
    if delta == 0:
        return f"Today {clock}"
    if delta == 1:
        return f"Yesterday {clock}"
    if delta < 7:
        return f"{d.strftime('%a' if short else '%A')} {clock}"
    if d.year == today.year:
        return f"{d.day} {d.strftime('%b')} {clock}"
    return f"{d.day} {d.strftime('%b %Y')} {clock}"


def fmt_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


REASONS = {
    "saved": "Saved",
    "before restore": "Kept before a restore",
    "restored": "Restored",
}


def show_in_explorer(path: str | Path) -> None:
    path = Path(path)
    if path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif path.parent.exists():
        os.startfile(str(path.parent))


# ---------- startup with Windows ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    main = Path(__file__).resolve().parents[1] / "main.py"
    return f'"{pyw}" "{main}" --tray'


def set_start_with_windows(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
            # Undo "Disabled" from Task Manager > Startup apps, which Windows stores separately.
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as a:
                    winreg.DeleteValue(a, APP_NAME)
            except FileNotFoundError:
                pass
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------- engine -> UI bridge ----------
class Bridge(QObject):
    backed_up = Signal(object)
    deleted = Signal(object)
    status = Signal(str)
    snapshot_done = Signal(str)
    update_ready = Signal(str, object)
    update_status = Signal(str)


class Thumbs:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.cache: dict[tuple[str, int], QPixmap] = {}
        self.provider = QFileIconProvider()

    def get(self, blob: str, path: str, size: int) -> QPixmap:
        key = (blob, size)
        if key in self.cache:
            return self.cache[key]
        tp = self.vault.thumb_path(blob)
        pm = QPixmap(str(tp)) if tp.exists() else QPixmap()
        if pm.isNull():
            from PySide6.QtCore import QFileInfo
            pm = self.provider.icon(QFileInfo(path)).pixmap(size, size)
            return pm
        pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.cache[key] = pm
        return pm


# ---------- pages ----------
class FilesPage(QWidget):
    def __init__(self, vault: Vault, thumbs: Thumbs, deleted: bool):
        super().__init__()
        self.setObjectName("page")
        self.vault = vault
        self.thumbs = thumbs
        self.deleted = deleted
        self.current: FileRow | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        header = QLabel("Deleted files" if deleted else "Your files")
        header.setObjectName("title")
        sub = QLabel(
            "Files that were deleted. ArtSafe kept a copy, so you can put them back."
            if deleted else
            "Every time you save, ArtSafe keeps a copy. Pick a file to see its older versions."
        )
        sub.setObjectName("muted")
        outer.addWidget(header)
        outer.addWidget(sub)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 10, 10, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name or folder…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.list = QListWidget()
        self.list.setIconSize(QSize(56, 56))
        self.list.setUniformItemSizes(True)
        self.list.currentItemChanged.connect(self._file_selected)
        self.empty = QLabel()
        self.empty.setObjectName("empty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        lv.addWidget(self.search)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.empty, 1)
        split.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(18, 16, 18, 16)
        self.name = QLabel("")
        self.name.setObjectName("title")
        self.name.setWordWrap(True)
        self.where = QLabel("")
        self.where.setObjectName("muted")
        self.where.setWordWrap(True)
        self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setStyleSheet("background:#f3f2f8; border-radius:10px;")
        vlabel = QLabel("Versions (newest first)")
        vlabel.setStyleSheet("font-weight:600; margin-top:8px;")
        self.versions = QListWidget()
        self.versions.setIconSize(QSize(48, 48))
        self.versions.currentItemChanged.connect(self._version_selected)
        self.versions.itemDoubleClicked.connect(lambda _: self._restore())

        buttons = QHBoxLayout()
        self.restore_btn = QPushButton("Put this file back" if deleted else "Restore this version")
        self.restore_btn.setObjectName("primary")
        self.restore_btn.clicked.connect(self._restore)
        self.copy_btn = QPushButton("Save as a copy…")
        self.copy_btn.clicked.connect(self._save_copy)
        self.folder_btn = QPushButton("Show in folder")
        self.folder_btn.clicked.connect(lambda: self.current and show_in_explorer(self.current.path))
        buttons.addWidget(self.restore_btn)
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.folder_btn)

        rv.addWidget(self.name)
        rv.addWidget(self.where)
        rv.addWidget(self.preview, 3)
        rv.addWidget(vlabel)
        rv.addWidget(self.versions, 2)
        rv.addLayout(buttons)
        self.detail = right
        split.addWidget(right)
        split.setSizes([430, 620])
        self._show_detail(None)

    # --- data ---
    def refresh(self) -> None:
        keep = self.current.id if self.current else None
        files = self.vault.list_files(self.deleted, self.search.text().strip())
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        select = None
        for f in files:
            when = fmt_time(f.deleted_at if self.deleted else f.last_backup, short=True)
            label = "Deleted" if self.deleted else "Saved"
            n = f.version_count
            item = QListWidgetItem(
                QIcon(self.thumbs.get(f.latest_blob, f.path, 56)),
                f"{f.name}\n{label} {when} · {n} version{'s' if n != 1 else ''}",
            )
            item.setToolTip(f.path)
            item.setData(Qt.UserRole, f)
            item.setSizeHint(QSize(0, 68))
            self.list.addItem(item)
            if f.id == keep:
                select = item
        self.list.blockSignals(False)

        has = bool(files)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            if self.search.text():
                self.empty.setText("Nothing matches your search.")
            elif self.deleted:
                self.empty.setText("No deleted files. 🎉\n\nIf you ever delete something by accident, it will show up here.")
            else:
                self.empty.setText("No backups yet.\n\nAdd the folders you work in under Settings, and ArtSafe will start protecting them.")
        if select:
            self.list.setCurrentItem(select)
            self.list.verticalScrollBar().setValue(scroll)
            self._load_versions(select.data(Qt.UserRole), keep_selection=True)
        elif has:
            self.list.setCurrentRow(0)
        else:
            self._show_detail(None)

    def _file_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        self._load_versions(item.data(Qt.UserRole) if item else None)

    def _load_versions(self, f: FileRow | None, keep_selection: bool = False) -> None:
        prev_version = None
        if keep_selection and self.versions.currentItem():
            prev_version = self.versions.currentItem().data(Qt.UserRole).id
        self.current = f
        self._show_detail(f)
        if not f:
            return
        self.name.setText(f.name)
        where = f"In {f.folder}"
        if self.deleted:
            where = f"Was in {f.folder} · deleted {fmt_time(f.deleted_at)}"
        self.where.setText(where)
        self.versions.blockSignals(True)
        self.versions.clear()
        target = None
        for i, v in enumerate(self.vault.list_versions(f.id)):
            tag = "  ← current" if i == 0 and not self.deleted else ""
            item = QListWidgetItem(
                QIcon(self.thumbs.get(v.blob, v.path, 48)),
                f"{fmt_time(v.backed_up_at)}{tag}\n{REASONS.get(v.reason, v.reason)} · {fmt_size(v.size)}",
            )
            item.setData(Qt.UserRole, v)
            item.setSizeHint(QSize(0, 58))
            self.versions.addItem(item)
            if v.id == prev_version:
                target = item
        self.versions.blockSignals(False)
        if target:
            self.versions.setCurrentItem(target)
        else:
            self.versions.setCurrentRow(1 if self.versions.count() > 1 and not self.deleted else 0)

    def _show_detail(self, f) -> None:
        for w in (self.name, self.where, self.preview, self.versions,
                  self.restore_btn, self.copy_btn, self.folder_btn):
            w.setEnabled(f is not None)
        if f is None:
            self.name.setText("")
            self.where.setText("")
            self.preview.clear()
            self.versions.clear()

    def _version_selected(self, item, _prev=None) -> None:
        if not item:
            self.preview.clear()
            return
        v: VersionRow = item.data(Qt.UserRole)
        tp = self.vault.thumb_path(v.blob)
        pm = QPixmap(str(tp)) if tp.exists() else QPixmap()
        if pm.isNull():
            self.preview.setText("No preview for this file type")
        else:
            box = self.preview.size()
            self.preview.setPixmap(pm.scaled(box.width() - 20, box.height() - 20,
                                             Qt.KeepAspectRatio, Qt.SmoothTransformation))
        is_current = self.versions.row(item) == 0 and not self.deleted
        self.restore_btn.setEnabled(not is_current)
        self.restore_btn.setToolTip("This is already the current version." if is_current else "")

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.versions.currentItem():
            self._version_selected(self.versions.currentItem())

    # --- actions ---
    def _selected_version(self) -> VersionRow | None:
        item = self.versions.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _restore(self) -> None:
        v = self._selected_version()
        if not v or not self.restore_btn.isEnabled():
            return
        name = Path(v.path).name
        if self.deleted:
            msg = f"Put “{name}” back in its folder?\n\n{Path(v.path).parent}"
        else:
            msg = (f"Replace “{name}” with the version from {fmt_time(v.backed_up_at)}?\n\n"
                   "No need to worry: the file as it is right now is kept as a version too, "
                   "so you can always switch back.")
        if Path(v.path).exists() and self.deleted:
            msg += "\n\nA file with this name is already there. It will be kept as a version first."
        if QMessageBox.question(self, APP_NAME, msg) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            target = self.vault.restore(v.id)
            log.info("Restored %s", target)
        except PermissionError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME,
                                f"Couldn't replace “{name}” because another program is using it.\n\n"
                                "Close it in Photoshop (or wherever it's open) and try again.")
            return
        except DiskFull:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME,
                                "Your disk is almost full, so ArtSafe can't safely keep a copy of the current "
                                "file before restoring. Nothing was changed.\n\n"
                                "Free up some space (or use “Save as a copy…” to another drive) and try again.")
            return
        except Exception as e:
            QApplication.restoreOverrideCursor()
            log.exception("Restore failed for %s", v.path)
            QMessageBox.warning(self, APP_NAME, f"Something went wrong while restoring:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        self.window().refresh_all()
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(f"Done! “{name}” is back.")
        box.setInformativeText("If it's open in Photoshop, close it there without saving and open it again.")
        show = box.addButton("Show in folder", QMessageBox.ActionRole)
        box.addButton("OK", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() == show:
            show_in_explorer(target)

    def _save_copy(self) -> None:
        v = self._selected_version()
        if not v:
            return
        p = Path(v.path)
        stamp = datetime.fromtimestamp(v.backed_up_at).strftime("%Y-%m-%d %H.%M")
        suggested = p.parent / f"{p.stem} (from {stamp}){p.suffix}"
        if not p.parent.exists():
            suggested = Path.home() / "Desktop" / suggested.name
        dest, _ = QFileDialog.getSaveFileName(self, "Save a copy", str(suggested), f"*{p.suffix}")
        if not dest:
            return
        try:
            self.vault.save_copy(v.id, dest)
        except Exception as e:
            log.exception("Save a copy failed")
            QMessageBox.warning(self, APP_NAME, f"Couldn't save the copy:\n{e}")
            return
        show_in_explorer(dest)


class AutosavesPage(QWidget):
    """Hidden auto-save copies Photoshop / Clip Studio made of open documents."""

    def __init__(self, vault: Vault, thumbs: Thumbs):
        super().__init__()
        self.setObjectName("page")
        self.vault = vault
        self.thumbs = thumbs

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        header = QLabel("Unsaved work")
        header.setObjectName("title")
        sub = QLabel(
            "While you work, ArtSafe takes a snapshot of what's open in Photoshop every few minutes, "
            "and keeps the backups Photoshop and Clip Studio make themselves. Use these to get back "
            "work you never saved, or from before you saved over something by accident."
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(header)
        titles.addWidget(sub)
        top.addLayout(titles, 1)
        self.snap_btn = QPushButton("📸  Snapshot Photoshop now")
        self.snap_btn.setObjectName("primary")
        self.snap_btn.setToolTip("Save a copy of every file open in Photoshop right now. Your real files aren't touched.")
        self.snap_btn.clicked.connect(self._snapshot_now)
        top.addWidget(self.snap_btn, 0, Qt.AlignTop)
        outer.addLayout(top)
        self.snapshotter: Snapshotter | None = None
        self.known: frozenset[str] = frozenset()

        split = QSplitter()
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 10, 10, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by artwork name…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        lv.addWidget(self.search)
        self.list = QListWidget()
        self.list.setIconSize(QSize(72, 72))
        self.list.setUniformItemSizes(True)
        self.list.currentItemChanged.connect(self._selected)
        self.list.itemDoubleClicked.connect(lambda _: self._save_as())
        self.empty = QLabel(
            "No auto-saves yet.\n\n"
            "Snapshots appear here every few minutes while you have unsaved changes in Photoshop, "
            "together with the backups Photoshop and Clip Studio make themselves."
        )
        self.empty.setObjectName("empty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.empty, 1)
        split.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(18, 16, 18, 16)
        self.title = QLabel("")
        self.title.setObjectName("title")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(300)
        self.preview.setStyleSheet("background:#f3f2f8; border-radius:10px;")
        tip = QLabel("Saving it creates a new file, so nothing you have now is touched. "
                     "Then open it in Photoshop or Clip Studio and copy over what you need.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save as a file…")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save_as)
        buttons.addWidget(self.save_btn)
        buttons.addStretch(1)
        rv.addWidget(self.title)
        rv.addWidget(self.info)
        rv.addWidget(self.preview, 1)
        rv.addWidget(tip)
        rv.addLayout(buttons)
        self.detail = right
        split.addWidget(right)
        split.setSizes([430, 620])

    def refresh(self) -> None:
        keep = self.list.currentItem().data(Qt.UserRole).id if self.list.currentItem() else None
        items = self.vault.list_autosaves()
        # her own file names, to turn Clip Studio's "<folder><file>" backup names back into real names
        self.known = frozenset(Path(f.path).name for d in (False, True) for f in self.vault.list_files(d))
        q = self.search.text().strip().lower()
        if q:
            items = [v for v in items if q in self._name(v).lower()]
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        select = None
        for v in items:
            item = QListWidgetItem(QIcon(self.thumbs.get(v.blob, v.path, 72)), self._label(v))
            item.setData(Qt.UserRole, v)
            item.setSizeHint(QSize(0, 84))
            item.setToolTip(Path(v.path).name)
            self.list.addItem(item)
            if v.id == keep:
                select = item
        self.list.blockSignals(False)
        has = bool(items)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        self.detail.setEnabled(has)
        if select:
            self.list.setCurrentItem(select)
            self.list.verticalScrollBar().setValue(scroll)
        elif has:
            self.list.setCurrentRow(0)
        else:
            self.title.setText("")
            self.info.setText("")
            self.preview.clear()

    def _name(self, v: VersionRow) -> str:
        return display_name(v.path) if v.app == SNAPSHOT_APP else describe(v.path, v.app, self.known)[0]

    def _kind(self, v: VersionRow) -> str:
        if v.app == SNAPSHOT_APP:
            return "Snapshot taken by ArtSafe while the file was open in Photoshop"
        return describe(v.path, v.app, self.known)[1]

    def _label(self, v: VersionRow) -> str:
        kind = self._kind(v)
        short = ("ArtSafe snapshot" if v.app == SNAPSHOT_APP else
                 "Clip Studio backup" if "from a save" in kind else
                 "Clip Studio first copy" if "first opened" in kind else
                 "Clip Studio recovery" if v.app == "Clip Studio" else
                 "Photoshop auto-save")
        return (f"{self._name(v)}  ·  {fmt_time(v.backed_up_at, short=True)}\n"
                f"{short} · {fmt_size(v.size)}")

    def _selected(self, item, _prev=None) -> None:
        if not item:
            return
        v: VersionRow = item.data(Qt.UserRole)
        self.title.setText(f"{self._name(v)} · {fmt_time(v.backed_up_at)}")
        self.info.setText(f"{self._kind(v)} · {fmt_size(v.size)} · file “{Path(v.path).name}”")
        self._show_preview(v)

    def _show_preview(self, v: VersionRow) -> None:
        tp = self.vault.thumb_path(v.blob)
        pm = QPixmap(str(tp)) if tp.exists() else QPixmap()
        if pm.isNull():
            self.preview.setText("No preview available.\nSave it as a file and open it to see what's inside.")
        else:
            box = self.preview.size()
            self.preview.setPixmap(pm.scaled(box.width() - 20, box.height() - 20,
                                             Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.list.currentItem():
            self._show_preview(self.list.currentItem().data(Qt.UserRole))

    def _snapshot_now(self) -> None:
        if not self.snapshotter:
            return
        self.snap_btn.setEnabled(False)
        self.snap_btn.setText("Taking snapshot…")
        self.snapshotter.snapshot_now(done=self.window().bridge.snapshot_done.emit)
        # never leave the button stuck if Photoshop doesn't answer
        QTimer.singleShot(120_000, lambda: self.snapshot_finished("") if not self.snap_btn.isEnabled() else None)

    def snapshot_finished(self, msg: str) -> None:
        self.snap_btn.setEnabled(True)
        self.snap_btn.setText("📸  Snapshot Photoshop now")
        self.refresh()

    def _save_as(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        v: VersionRow = item.data(Qt.UserRole)
        ext = Path(v.path).suffix or ".psb"
        stamp = datetime.fromtimestamp(v.backed_up_at).strftime("%Y-%m-%d %H.%M")
        suggested = Path.home() / "Desktop" / f"{self._name(v)} (recovered {stamp}){ext}"
        dest, _ = QFileDialog.getSaveFileName(self, "Save the recovered work", str(suggested), f"*{ext}")
        if not dest:
            return
        try:
            self.vault.save_copy(v.id, dest)
        except Exception as e:
            log.exception("Saving recovered work failed")
            QMessageBox.warning(self, APP_NAME, f"Couldn't save it:\n{e}")
            return
        show_in_explorer(dest)


class SettingsPage(QWidget):
    changed = Signal()

    def __init__(self, cfg: Config, vault: Vault):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        self.vault = vault
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Settings")
        title.setObjectName("title")
        outer.addWidget(title)

        # folders
        g = QGroupBox("Folders to protect")
        gl = QVBoxLayout(g)
        hint = QLabel("Everything inside these folders (and their subfolders) is backed up whenever it changes.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.folders = QListWidget()
        self.folders.setMinimumHeight(90)
        self.folders.setMaximumHeight(160)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.setObjectName("primary")
        add.clicked.connect(self._add_folder)
        rem = QPushButton("Remove")
        rem.clicked.connect(self._remove_folder)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        gl.addWidget(hint)
        gl.addWidget(self.folders)
        gl.addLayout(row)
        outer.addWidget(g)

        # cloud
        g = QGroupBox("Cloud copy")
        gl = QVBoxLayout(g)
        self.cloud_on = QCheckBox("Also keep a copy of every file in a cloud folder (OneDrive, Google Drive, Dropbox…)")
        self.cloud_on.toggled.connect(self._cloud_changed)
        crow = QHBoxLayout()
        self.cloud_path = QLineEdit()
        self.cloud_path.setReadOnly(True)
        pick = QPushButton("Choose…")
        pick.clicked.connect(self._pick_cloud)
        crow.addWidget(self.cloud_path, 1)
        crow.addWidget(pick)
        self.cloud_hint = QLabel()
        self.cloud_hint.setObjectName("muted")
        self.cloud_hint.setWordWrap(True)
        gl.addWidget(self.cloud_on)
        gl.addLayout(crow)
        gl.addWidget(self.cloud_hint)
        outer.addWidget(g)

        # app auto-saves
        g = QGroupBox("Unsaved work (Photoshop)")
        gl = QVBoxLayout(g)
        srow = QHBoxLayout()
        self.snap_on = QCheckBox("Take a snapshot of open Photoshop files every")
        self.snap_on.toggled.connect(self._snap_changed)
        self.snap_minutes = QSpinBox(); self.snap_minutes.setRange(1, 120); self.snap_minutes.setSuffix(" minutes")
        self.snap_minutes.setMaximumWidth(180)
        self.snap_minutes.valueChanged.connect(self._snap_changed)
        srow.addWidget(self.snap_on)
        srow.addWidget(self.snap_minutes)
        srow.addStretch(1)
        snap_hint = QLabel("Only when there are unsaved changes, and only during a short pause in your work, "
                           "so Photoshop never freezes mid-stroke. Your real file is never touched.")
        snap_hint.setObjectName("muted")
        snap_hint.setWordWrap(True)
        gl.addLayout(srow)
        gl.addWidget(snap_hint)
        self.autosave_on = QCheckBox("Also keep the auto-saves Photoshop and Clip Studio make themselves (recommended)")
        self.autosave_on.toggled.connect(self._autosave_changed)
        arow = QHBoxLayout()
        arow.addWidget(QLabel("Keep snapshots and auto-saves for"))
        self.keep_autosave = QSpinBox(); self.keep_autosave.setRange(1, 365); self.keep_autosave.setSuffix(" days")
        self.keep_autosave.setMaximumWidth(180)
        self.keep_autosave.valueChanged.connect(self._retention_changed)
        arow.addWidget(self.keep_autosave)
        arow.addStretch(1)
        self.autosave_hint = QLabel()
        self.autosave_hint.setObjectName("muted")
        self.autosave_hint.setWordWrap(True)
        gl.addWidget(self.autosave_on)
        gl.addLayout(arow)
        gl.addWidget(self.autosave_hint)
        outer.addWidget(g)

        # retention
        g = QGroupBox("How long to keep old versions")
        form = QFormLayout(g)
        self.keep_all = QSpinBox(); self.keep_all.setRange(1, 3650); self.keep_all.setSuffix(" days")
        self.keep_daily = QSpinBox(); self.keep_daily.setRange(1, 3650); self.keep_daily.setSuffix(" days")
        self.keep_deleted = QSpinBox(); self.keep_deleted.setRange(1, 36500); self.keep_deleted.setSuffix(" days")
        self.max_gb = QDoubleSpinBox(); self.max_gb.setRange(0, 100000); self.max_gb.setSuffix(" GB")
        self.max_gb.setSpecialValueText("No limit")
        form.addRow("Keep every saved version for", self.keep_all)
        form.addRow("After that, keep one per day for", self.keep_daily)
        form.addRow("Keep deleted files for", self.keep_deleted)
        form.addRow("Maximum backup size", self.max_gb)
        note = QLabel("The newest copy of every file is always kept, whatever these limits say.")
        note.setObjectName("muted")
        form.addRow(note)
        for w in (self.keep_all, self.keep_daily, self.keep_deleted, self.max_gb):
            w.setMaximumWidth(180)
            w.valueChanged.connect(self._retention_changed)
        outer.addWidget(g)

        # general
        g = QGroupBox("General")
        gl = QVBoxLayout(g)
        self.autostart = QCheckBox("Start ArtSafe automatically when Windows starts (recommended)")
        self.autostart.toggled.connect(self._autostart_changed)
        self.send_reports = QCheckBox("Send error reports to the developer, so problems get fixed (recommended)")
        self.send_reports.toggled.connect(self._send_reports_changed)
        self.send_reports.setVisible(logs.reporting_available())
        self.auto_update = QCheckBox("Keep ArtSafe up to date automatically (recommended)")
        self.auto_update.toggled.connect(self._auto_update_changed)
        urow = QHBoxLayout()
        self.version_label = QLabel(f"Version {__version__}")
        self.version_label.setObjectName("muted")
        self.check_updates = QPushButton("Check for updates")
        self.check_updates.clicked.connect(lambda: self.window().check_updates_now())
        urow.addWidget(self.version_label)
        urow.addWidget(self.check_updates)
        urow.addStretch(1)
        self.usage = QLabel()
        self.usage.setObjectName("muted")
        brow = QHBoxLayout()
        open_vault = QPushButton("Open backup folder")
        open_vault.clicked.connect(lambda: os.startfile(self.cfg.vault_dir))
        report = QPushButton("Something wrong? Send a report…")
        report.clicked.connect(self._send_report)
        report.setVisible(logs.reporting_available())
        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(lambda: os.startfile(str(logs.logs_dir())))
        setup = QPushButton("Run setup again…")
        setup.clicked.connect(lambda: self.window().run_setup())
        brow.addWidget(open_vault)
        brow.addWidget(setup)
        brow.addWidget(open_logs)
        brow.addWidget(report)
        brow.addStretch(1)
        gl.addWidget(self.autostart)
        gl.addWidget(self.auto_update)
        gl.addWidget(self.send_reports)
        gl.addLayout(urow)
        gl.addWidget(self.usage)
        gl.addLayout(brow)
        outer.addWidget(g)
        outer.addStretch(1)
        self.load()

    def load(self) -> None:
        widgets = (self.cloud_on, self.keep_all, self.keep_daily, self.keep_deleted, self.max_gb,
                   self.autostart, self.autosave_on, self.keep_autosave, self.snap_on, self.snap_minutes,
                   self.auto_update, self.send_reports)
        for w in widgets:
            w.blockSignals(True)
        self.auto_update.setChecked(self.cfg.auto_update)
        self.send_reports.setChecked(self.cfg.send_error_reports)
        self.snap_on.setChecked(self.cfg.ps_snapshots)
        self.snap_minutes.setValue(self.cfg.ps_snapshot_minutes)
        self.snap_minutes.setEnabled(self.cfg.ps_snapshots)
        self.autosave_on.setChecked(self.cfg.capture_autosaves)
        self.keep_autosave.setValue(self.cfg.keep_autosave_days)
        self.keep_autosave.setEnabled(self.cfg.capture_autosaves or self.cfg.ps_snapshots)
        sources = find_sources()
        if not self.cfg.capture_autosaves:
            self.autosave_hint.setText("Off.")
        elif sources:
            apps = sorted({s.app for s in sources})
            tips = []
            if "Photoshop" in apps:
                tips.append("In Photoshop, set Edit → Preferences → File Handling → "
                            "“Automatically Save Recovery Information Every” to 5 minutes.")
            if "Clip Studio" in apps:
                tips.append("In Clip Studio, tick File → Preferences → File → Auto-Recovery → "
                            "“Enable canvas recovery”.")
            self.autosave_hint.setText(
                f"Keeping backups from {' and '.join(apps)}. " + " ".join(tips) +
                " These stay on this PC only (they're too big to upload every few minutes).")
        else:
            self.autosave_hint.setText("Photoshop / Clip Studio not found yet. ArtSafe will notice once they're installed.")
        self.folders.clear()
        for f in self.cfg.watched_folders:
            item = QListWidgetItem(("📁  " if os.path.isdir(f) else "⚠️  ") + f)
            item.setData(Qt.UserRole, f)
            if not os.path.isdir(f):
                item.setToolTip("This folder can't be found right now (unplugged drive or renamed?).")
            self.folders.addItem(item)
        self.cloud_on.setChecked(self.cfg.cloud_enabled)
        self.cloud_path.setText(self.cfg.cloud_dir or "No folder chosen")
        self.keep_all.setValue(self.cfg.keep_all_days)
        self.keep_daily.setValue(self.cfg.keep_daily_days)
        self.keep_deleted.setValue(self.cfg.keep_deleted_days)
        self.max_gb.setValue(self.cfg.max_vault_gb)
        self.autostart.setChecked(self.cfg.start_with_windows)
        for w in widgets:
            w.blockSignals(False)
        self._update_cloud_hint()
        self.update_usage()

    def update_usage(self) -> None:
        s = self.vault.stats()
        self.usage.setText(
            f"Backups use {fmt_size(s['bytes'])} on this PC · {s['files']} files · {s['versions']} versions\n"
            f"Stored in {self.cfg.vault_dir}"
        )

    def _save(self, reload_engine: bool = True) -> None:
        self.cfg.save()
        if reload_engine:
            self.changed.emit()

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a folder to protect", str(Path.home()))
        if not d:
            return
        d = os.path.normpath(d)
        if d not in self.cfg.watched_folders:
            self.cfg.watched_folders.append(d)
            self._save()
            self.load()

    def _remove_folder(self) -> None:
        item = self.folders.currentItem()
        if not item:
            return
        f = item.data(Qt.UserRole)
        if QMessageBox.question(
            self, APP_NAME,
            f"Stop protecting this folder?\n\n{f}\n\nBackups already made are kept."
        ) != QMessageBox.Yes:
            return
        self.cfg.watched_folders.remove(f)
        self._save()
        self.load()

    def _pick_cloud(self) -> None:
        clouds = detect_cloud_folders()
        start = self.cfg.cloud_dir or str(next(iter(clouds.values()), Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Choose a folder inside OneDrive / Google Drive / Dropbox", start)
        if not d:
            return
        d = os.path.normpath(d)
        if Path(d).name != "ArtSafe Backup":
            d = os.path.join(d, "ArtSafe Backup")
        self.cfg.cloud_dir = d
        self.cfg.cloud_enabled = True
        self._save()
        self.load()

    def _cloud_changed(self, on: bool) -> None:
        if on and not self.cfg.cloud_dir:
            clouds = detect_cloud_folders()
            if clouds:
                self.cfg.cloud_dir = str(next(iter(clouds.values())) / "ArtSafe Backup")
            else:
                self.cloud_on.blockSignals(True)
                self.cloud_on.setChecked(False)
                self.cloud_on.blockSignals(False)
                self._pick_cloud()
                return
        self.cfg.cloud_enabled = on
        self._save()
        self.load()

    def _update_cloud_hint(self) -> None:
        if not self.cfg.cloud_enabled:
            clouds = detect_cloud_folders()
            found = ", ".join(clouds) if clouds else "none found on this PC"
            self.cloud_hint.setText(f"Off. Cloud apps found: {found}.")
        else:
            self.cloud_hint.setText(
                "The newest copy of each file is placed in this folder and your cloud app uploads it. "
                "Deleting a file in your art folders never deletes it here. "
                "Make sure the cloud account has enough space for your files."
            )

    def _retention_changed(self) -> None:
        self.cfg.keep_all_days = self.keep_all.value()
        self.cfg.keep_daily_days = max(self.keep_daily.value(), self.keep_all.value())
        self.cfg.keep_deleted_days = self.keep_deleted.value()
        self.cfg.max_vault_gb = self.max_gb.value()
        self.cfg.keep_autosave_days = self.keep_autosave.value()
        self._save(reload_engine=False)

    def _send_reports_changed(self, on: bool) -> None:
        self.cfg.send_error_reports = on
        self._save(reload_engine=False)

    def _send_report(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        note, ok = QInputDialog.getMultiLineText(
            self, "Send a report", "What happened? (optional)\n\nArtSafe's log file is sent along with it.")
        if not ok:
            return
        if logs.send_report(note):
            QMessageBox.information(self, APP_NAME, "Thanks! The report was sent.")
        else:
            QMessageBox.warning(self, APP_NAME, "The report couldn't be sent. Use “Open log folder” and send "
                                                "the artsafe.log file in a chat instead.")

    def _auto_update_changed(self, on: bool) -> None:
        self.cfg.auto_update = on
        self._save(reload_engine=False)

    def _snap_changed(self, *_):
        self.cfg.ps_snapshots = self.snap_on.isChecked()
        self.cfg.ps_snapshot_minutes = self.snap_minutes.value()
        self.snap_minutes.setEnabled(self.cfg.ps_snapshots)
        self._save(reload_engine=False)

    def _autosave_changed(self, on: bool) -> None:
        self.cfg.capture_autosaves = on
        self._save()
        self.load()

    def _autostart_changed(self, on: bool) -> None:
        if not on and QMessageBox.question(
            self, APP_NAME,
            "If ArtSafe doesn't start with Windows, your files aren't protected until you open it yourself.\n\n"
            "Turn this off anyway?"
        ) != QMessageBox.Yes:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(True)
            self.autostart.blockSignals(False)
            return
        self.cfg.start_with_windows = on
        try:
            set_start_with_windows(on)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't change the startup setting:\n{e}")
        self._save(reload_engine=False)


# ---------- main window ----------
class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, vault: Vault, engine: Engine, bridge: Bridge):
        super().__init__()
        self.cfg, self.vault, self.engine, self.bridge = cfg, vault, engine, bridge
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(1180, 760)
        self.thumbs = Thumbs(vault)
        self._quitting = False
        self._told_about_tray = False

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.setCentralWidget(root)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(230)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 14)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        self.nav = QListWidget()
        for text in ("🖼️   Your files", "🗑️   Deleted files", "⏳   Unsaved work", "⚙️   Settings"):
            self.nav.addItem(text)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.activity = QLabel()
        self.activity.setObjectName("muted")
        self.activity.setWordWrap(True)
        self.activity.setContentsMargins(14, 0, 14, 0)
        sv.addWidget(brand)
        sv.addWidget(self.nav, 1)
        box = QVBoxLayout()
        box.setContentsMargins(12, 0, 12, 0)
        box.addWidget(self.status)
        sv.addLayout(box)
        sv.addWidget(self.activity)
        h.addWidget(side)

        self.stack = QStackedWidget()
        self.files_page = FilesPage(vault, self.thumbs, deleted=False)
        self.deleted_page = FilesPage(vault, self.thumbs, deleted=True)
        self.settings_page = SettingsPage(cfg, vault)
        self.settings_page.changed.connect(self._settings_changed)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(self.settings_page)
        self.autosaves_page = AutosavesPage(vault, self.thumbs)
        for p in (self.files_page, self.deleted_page, self.autosaves_page, settings_scroll):
            self.stack.addWidget(p)
        h.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self._go)
        self.nav.setCurrentRow(0)

        # refresh lists shortly after backups happen, not on every single one
        self._refresh_timer = QTimer(self, singleShot=True, interval=800)
        self._refresh_timer.timeout.connect(self.refresh_all)
        bridge.backed_up.connect(lambda _: self._refresh_timer.start())
        bridge.deleted.connect(lambda _: self._refresh_timer.start())
        bridge.status.connect(self._activity)
        self._update_status()

    def _go(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        page = self.stack.currentWidget()
        if isinstance(page, (FilesPage, AutosavesPage)):
            page.refresh()
        else:
            self.settings_page.load()

    def check_updates_now(self) -> None:
        self.settings_page.version_label.setText(f"Version {__version__} · checking…")
        self.updater.check_now()

    def run_setup(self) -> None:
        from .onboarding import Onboarding

        if Onboarding(self.cfg, self).exec():
            self.settings_page.load()
            self._settings_changed()
            self.refresh_all()

    def go_deleted(self) -> None:
        self.nav.setCurrentRow(1)

    def go_settings(self) -> None:
        self.nav.setCurrentRow(3)

    def refresh_all(self) -> None:
        page = self.stack.currentWidget()
        if isinstance(page, (FilesPage, AutosavesPage)):
            page.refresh()
        else:
            self.settings_page.update_usage()
        self._update_status()

    def _activity(self, msg: str) -> None:
        self.activity.setText(msg)
        self._update_status()

    def _settings_changed(self) -> None:
        self.engine.reload()
        if self.cfg.cloud_enabled:
            threading.Thread(target=self.vault.sync_cloud_all, daemon=True).start()
        self._update_status()

    def _update_status(self) -> None:
        folders = [f for f in self.cfg.watched_folders if os.path.isdir(f)]
        if self.engine.disk_full:
            text, bg, fg = "⛔  Disk almost full: backups paused. Free up some space.", "#fde8e8", "#9b1c1c"
        elif not self.cfg.watched_folders:
            text, bg, fg = "⚠️  Not protecting anything yet. Add a folder in Settings.", "#fff4e0", "#8a5300"
        elif len(folders) < len(self.cfg.watched_folders):
            text, bg, fg = "⚠️  A protected folder can't be found. Check Settings.", "#fff4e0", "#8a5300"
        else:
            cloud = " + cloud" if self.cfg.cloud_enabled else ""
            text, bg, fg = f"✓  Protected{cloud}", "#e6f7ec", "#17703a"
        self.status.setText(text)
        self.status.setStyleSheet(f"background:{bg}; color:{fg};")

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e) -> None:
        if self._quitting:
            e.accept()
            return
        e.ignore()
        self.hide()
        if not self._told_about_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._told_about_tray = True
            self.tray.showMessage(APP_NAME, "ArtSafe keeps protecting your files in the background. "
                                  "Click the shield icon near the clock to open it.", make_icon(), 5000)


# ---------- app ----------
def report_update_result(just_updated: bool) -> None:
    """Tell the developer whether the last automatic update worked (from the updater's log)."""
    f = logs.logs_dir() / "update.log"
    try:
        if not f.exists() or time.time() - f.stat().st_mtime > 600:
            return
        last = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    except (OSError, IndexError):
        return
    if just_updated:
        logs.event("Update installed", f"Now running version {__version__}")
    elif "could not" in last.lower():
        logs.event("Update failed", last)


def run(argv: list[str]) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    use_light_theme(app)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(app_style())
    app.setWindowIcon(make_icon())

    # only one copy runs; starting it again just opens the window
    server_name = f"{APP_NAME}-{os.environ.get('USERNAME', 'user')}"
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    cfg = Config.load()
    logs.setup(report_enabled=lambda: cfg.send_error_reports)
    first_run = not cfg.first_run_done
    log.info("Config: %d folders, cloud=%s, snapshots=%s, autosaves=%s, limit=%s GB",
             len(cfg.watched_folders), cfg.cloud_enabled, cfg.ps_snapshots, cfg.capture_autosaves,
             cfg.max_vault_gb)
    vault = Vault(cfg)
    bridge = Bridge()
    engine = Engine(cfg, vault,
                    on_backup=bridge.backed_up.emit,
                    on_deleted=bridge.deleted.emit,
                    on_status=bridge.status.emit)
    win = MainWindow(cfg, vault, engine, bridge)
    snapshotter = Snapshotter(cfg, vault, on_backup=bridge.backed_up.emit, on_status=bridge.status.emit)
    win.autosaves_page.snapshotter = snapshotter
    upd = updater.Updater(enabled=lambda: cfg.auto_update,
                          on_ready=bridge.update_ready.emit, on_status=bridge.update_status.emit)
    win.updater = upd

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(f"{APP_NAME}: protecting your files")
    menu = QMenu()
    a_open = QAction("Open ArtSafe", menu)
    a_open.triggered.connect(win.show_and_raise)
    a_deleted = QAction("Recover a deleted file…", menu)
    a_deleted.triggered.connect(lambda: (win.go_deleted(), win.show_and_raise()))
    a_snap = QAction("Snapshot Photoshop now", menu)
    a_snap.triggered.connect(lambda: snapshotter.snapshot_now(done=bridge.snapshot_done.emit))
    menu.addAction(a_snap)
    a_unsaved = QAction("Recover unsaved work…", menu)
    a_unsaved.triggered.connect(lambda: (win.nav.setCurrentRow(2), win.show_and_raise()))
    menu.addAction(a_unsaved)
    a_scan = QAction("Check folders now", menu)
    a_scan.triggered.connect(engine.request_rescan)
    a_quit = QAction("Quit", menu)
    menu.addAction(a_open)
    menu.addAction(a_deleted)
    menu.addAction(a_scan)
    menu.addSeparator()
    menu.addAction(a_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: win.show_and_raise()
                           if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
    tray.show()
    win.tray = tray

    # gentle heads-up when files get deleted, grouped if many go at once
    pending_deleted: list[FileRow] = []
    flush = QTimer(singleShot=True, interval=2500)

    def on_deleted(f: FileRow) -> None:
        pending_deleted.append(f)
        flush.start()

    def flush_deleted() -> None:
        if not pending_deleted:
            return
        if len(pending_deleted) == 1:
            msg = f"“{pending_deleted[0].name}” was deleted. ArtSafe kept a copy. Click here if you want it back."
        else:
            msg = f"{len(pending_deleted)} files were deleted. ArtSafe kept copies. Click here if you want them back."
        pending_deleted.clear()
        tray._last_was_deleted = True
        tray.showMessage(APP_NAME, msg, make_icon(), 8000)

    tray._last_was_deleted = False

    def on_message_clicked() -> None:
        if tray._last_was_deleted:
            win.go_deleted()
        win.show_and_raise()
        tray._last_was_deleted = False

    flush.timeout.connect(flush_deleted)
    bridge.deleted.connect(on_deleted)
    tray.messageClicked.connect(on_message_clicked)

    def on_snapshot_done(msg: str) -> None:
        win.autosaves_page.snapshot_finished(msg)
        tray._last_was_deleted = False
        tray.showMessage(APP_NAME, msg, make_icon(), 4000)

    bridge.snapshot_done.connect(on_snapshot_done)
    bridge.status.connect(lambda s: tray.setToolTip(f"{APP_NAME}: {s}"[:120]))

    def on_new_connection() -> None:
        c = server.nextPendingConnection()
        c.waitForReadyRead(300)
        win.show_and_raise()

    server.newConnection.connect(on_new_connection)

    def quit_app() -> None:
        if QMessageBox.question(
            win, APP_NAME,
            "Quit ArtSafe?\n\nYour files won't be backed up until ArtSafe is started again "
            "(it starts by itself next time Windows starts)."
        ) != QMessageBox.Yes:
            return
        win._quitting = True
        tray.hide()
        snapshotter.stop()
        upd.stop()
        engine.stop()
        vault.close()
        app.quit()

    a_quit.triggered.connect(quit_app)

    # ----- updates: install only while the window is closed, so nothing she's doing is interrupted
    pending_update: list = []

    def on_update_status(msg: str) -> None:
        win.settings_page.version_label.setText(f"Version {__version__} · {msg}")

    def try_install_update() -> None:
        if not pending_update or win.isVisible():
            return
        version, folder = pending_update[0]
        try:
            updater.launch_apply(folder, version)
        except Exception as e:
            log.exception("Couldn't start installing update %s", version)
            on_update_status(f"couldn't install update: {e}")
            pending_update.clear()
            return
        log.info("Closing so update %s can be installed", version)
        win._quitting = True
        tray.hide()
        snapshotter.stop()
        upd.stop()
        engine.stop()
        vault.close()
        app.quit()

    def on_update_ready(version: str, folder) -> None:
        pending_update[:] = [(version, folder)]
        on_update_status(f"version {version} will be installed when this window is closed")
        try_install_update()

    install_timer = QTimer(interval=60_000)
    install_timer.timeout.connect(try_install_update)
    install_timer.start()
    bridge.update_status.connect(on_update_status)
    bridge.update_ready.connect(on_update_ready)

    if first_run:
        from .onboarding import Onboarding
        Onboarding(cfg).exec()  # saves her choices when she clicks Finish
    # Re-register on every launch so startup keeps working even if the app folder was moved.
    if cfg.start_with_windows:
        try:
            set_start_with_windows(True)
        except OSError:
            pass
    engine.start()
    snapshotter.start()
    upd.start()
    report_update_result("--updated" in argv)
    if "--updated" in argv:
        QTimer.singleShot(1500, lambda: tray.showMessage(
            APP_NAME, f"ArtSafe was updated to version {__version__}.", make_icon(), 5000))

    win.settings_page.load()
    if not cfg.watched_folders:
        win.go_settings()
        win.show_and_raise()
    elif first_run or "--tray" not in argv:
        win.show_and_raise()

    return app.exec()
