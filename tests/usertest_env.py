"""Shared setup for driving the real ArtSafe.exe in a throwaway profile."""
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "ArtSafe" / "ArtSafe.exe"
BASE = Path(os.environ.get("TEMP")) / "artsafe-usertest"
HOME = BASE / "Anna"
ART = HOME / "Desktop" / "Commissions"
ONEDRIVE = HOME / "OneDrive"


def env() -> dict:
    e = dict(os.environ)
    e.update({
        "USERPROFILE": str(HOME),
        "APPDATA": str(HOME / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(HOME / "AppData" / "Local"),
        "OneDrive": str(ONEDRIVE),
    })
    e.pop("OneDriveConsumer", None)
    e.pop("OneDriveCommercial", None)
    return e


def layered_psd(path: Path, size=(600, 400), layers=3):
    """A real multi-layer PSD, like an in-progress drawing."""
    psd = PSDImage.new("RGB", size)
    colors = ["#ff004d", "#ffa300", "#29adff", "#00e436", "#7e2553"]
    for i in range(layers):
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((40 + i * 120, 80, 240 + i * 120, 280), fill=colors[i % len(colors)])
        psd.append(PixelLayer.frompil(img, psd, f"Layer {i + 1}"))
    psd.save(path)


def flattened_psd(path: Path, size=(600, 400)):
    """The 'oops, merged everything' version: one flat grey layer."""
    PSDImage.frompil(Image.new("RGB", size, "#808080")).save(path)


def layer_count(path: Path) -> int:
    return len(list(PSDImage.open(path)))


def launch(*args) -> subprocess.Popen:
    return subprocess.Popen([str(EXE), *args], env=env())
