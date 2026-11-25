"""A .clip that turns into a .psd (Clip Studio export / Save As) must stay recoverable as a .clip."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from artsafe.config import Config
from artsafe.engine import Engine
from artsafe.vault import Vault


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    art = home / "Art"
    art.mkdir(parents=True)
    cfg = Config(watched_folders=[str(art)], vault_dir=str(tmp_path / "vault"))
    v = Vault(cfg)
    alerts = []
    e = Engine(cfg, v, on_deleted=alerts.append)
    yield cfg, v, e, art, alerts
    v.close()


def test_clip_replaced_by_psd_is_kept_as_a_deleted_clip(env):
    cfg, v, e, art, alerts = env
    clip, psd = art / "dragon.clip", art / "dragon.psd"
    clip.write_bytes(b"clip layers v1")
    e.full_scan()
    clip.write_bytes(b"clip layers v2, more work")
    e.full_scan()

    # what the watcher reports when the .clip becomes the .psd
    clip.unlink()
    psd.write_bytes(b"flattened psd export")
    e.handle_move(str(clip), str(psd))
    e.process_pending(settle=0)

    [gone] = v.list_files(deleted=True)
    assert gone.name == "dragon.clip" and gone.version_count == 2
    assert [a.name for a in alerts] == ["dragon.clip"]  # she's told
    [live] = v.list_files(deleted=False)
    assert live.name == "dragon.psd" and live.version_count == 1

    # and it really comes back, as a .clip, with the newest work in it
    newest = v.list_versions(gone.id)[0]
    v.restore(newest.id)
    assert clip.read_bytes() == b"clip layers v2, more work"
    assert psd.read_bytes() == b"flattened psd export"


def test_plain_rename_still_carries_history(env):
    cfg, v, e, art, alerts = env
    old, new = art / "dragon.clip", art / "dragon final.clip"
    old.write_bytes(b"clip layers")
    e.full_scan()
    old.rename(new)
    e.handle_move(str(old), str(new))
    e.process_pending(settle=0)
    assert alerts == [] and v.list_files(deleted=True) == []
    [f] = v.list_files(deleted=False)
    assert f.name == "dragon final.clip" and f.version_count == 1


def test_backups_misfiled_by_older_versions_are_recovered(env):
    """1.0.2 filed the .clip's versions under the .psd. Opening the vault puts them back."""
    cfg, v, e, art, alerts = env
    clip, psd = art / "dragon.clip", art / "dragon.psd"
    clip.write_bytes(b"clip layers v1")
    e.full_scan()
    clip.write_bytes(b"clip layers v2, more work")
    e.full_scan()
    clip.unlink()
    psd.write_bytes(b"flattened psd export")
    assert v.rename(clip, psd)  # what 1.0.2 did
    v.backup_file(psd)
    assert v.list_files(deleted=True) == []  # the bug: the .clip is nowhere to be found
    v.db.execute("PRAGMA user_version=0")
    v.db.commit()
    v.close()

    v2 = Vault(cfg)
    try:
        [gone] = v2.list_files(deleted=True)
        assert gone.name == "dragon.clip" and gone.version_count == 2
        [live] = v2.list_files(deleted=False)
        assert live.name == "dragon.psd" and live.version_count == 1
        v2.restore(v2.list_versions(gone.id)[0].id)
        assert clip.read_bytes() == b"clip layers v2, more work"
    finally:
        v2.close()
