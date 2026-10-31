import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image
from psd_tools import PSDImage

from artsafe import photoshop
from artsafe.config import Config
from artsafe.photoshop import LIST_JS, SAVE_JS, Snapshotter, display_name
from artsafe.vault import Vault


class FakePhotoshop:
    """Answers DoJavaScript the way Photoshop would, and records what it was asked."""

    def __init__(self, docs):
        self.docs = docs  # (name, full_path, saved, active, color)
        self.saves = []
        self.misbehave = False

    def DoJavaScript(self, js, args=None, mode=1):
        if js == LIST_JS:
            return "\n".join("\t".join([n, f, "1" if s else "0", "1" if a else "0"]) for n, f, s, a, _ in self.docs)
        name, target = [json.loads(x) for x in re.search(r"\}\)\((.*), (.*)\);\s*$", js).groups()]
        color = next(d[4] for d in self.docs if d[0] == name)
        if self.misbehave:  # Photoshop "forgot" it was a copy
            return "moved: " + target + ".psd"
        PSDImage.frompil(Image.new("RGB", (40, 30), color)).save(target + ".psd")
        self.saves.append(name)
        return target + ".psd"


@pytest.fixture
def snap(tmp_path, monkeypatch):
    cfg = Config(vault_dir=str(tmp_path / "vault"))
    vault = Vault(cfg)
    s = Snapshotter(cfg, vault)
    yield s, vault, monkeypatch
    vault.close()


def use(monkeypatch, s, fake):
    monkeypatch.setattr(s, "_connect", lambda: fake)


def test_auto_snapshot_only_active_unsaved_doc(snap):
    s, vault, mp = snap
    fake = FakePhotoshop([
        ("dragon.psd", r"C:\Art\dragon.psd", False, True, "red"),
        ("old.psd", r"C:\Art\old.psd", False, False, "blue"),
    ])
    use(mp, s, fake)
    mp.setattr(photoshop, "last_input_tick", lambda: 1)
    msg = s._take(all_docs=False, force=False)
    assert fake.saves == ["dragon.psd"] and "dragon.psd" in msg
    [v] = vault.list_autosaves()
    assert v.app == "Photoshop snapshot" and display_name(v.path) == "dragon"
    assert vault.thumb_path(v.blob).exists()
    assert not list((Path(s.cfg.vault_dir) / "staging").glob("*"))  # staging cleaned up

    # no mouse/keyboard input since -> nothing new to do
    s._take(all_docs=False, force=False)
    assert fake.saves == ["dragon.psd"]


def test_saved_docs_skipped_unless_manual(snap):
    s, vault, mp = snap
    fake = FakePhotoshop([("a.psd", r"C:\Art\a.psd", True, True, "red"),
                          ("Untitled-1", "", False, False, "green")])
    use(mp, s, fake)
    ticks = iter(range(100))
    mp.setattr(photoshop, "last_input_tick", lambda: next(ticks))
    assert "No unsaved" in s._take(all_docs=False, force=False)
    s._take(all_docs=True, force=True)  # "Snapshot now" takes everything
    assert sorted(fake.saves) == ["Untitled-1", "a.psd"]
    assert sorted(display_name(v.path) for v in vault.list_autosaves()) == ["Untitled-1", "a"]


def test_photoshop_not_open(snap):
    s, vault, mp = snap
    use(mp, s, None)
    assert s._take(all_docs=True, force=True) == "Photoshop isn't open"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_scripts_are_valid_javascript():
    for js in (LIST_JS, SAVE_JS % ('"a.psd"', '"C:/x/a [123456].psb"')):
        r = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
                           input=js, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_snapshot_stops_if_photoshop_moves_the_document(snap):
    s, vault, mp = snap
    s.cfg.ps_snapshots = True
    mp.setattr(s.cfg, "save", lambda: None)
    fake = FakePhotoshop([("dragon.psd", r"C:\Art\dragon.psd", False, True, "red")])
    fake.misbehave = True
    use(mp, s, fake)
    mp.setattr(photoshop, "last_input_tick", lambda: 7)
    msg = s._take(all_docs=False, force=False)
    assert "Save As" in msg and s.cfg.ps_snapshots is False
    assert vault.list_autosaves() == []
