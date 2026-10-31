"""Cleaning up backups must never touch her art: only ArtSafe's own copies in the backup folder."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artsafe.config import DRAWING_EXTENSIONS, Config
from artsafe.vault import Vault


def snapshot(root: Path) -> dict:
    """Every file under root with its exact content hash."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def setup(tmp_path):
    home = tmp_path / "Art"
    files = {
        "Old/dragon.psd": b"8BPS dragon",
        "Old/sub/cat.clip": b"CSFCHUNK cat",
        "Old/same.psd": b"8BPS shared content",
        "Old/Current/fox.psd": b"8BPS fox",       # a folder inside Old that's still protected
        "Old2/owl.psd": b"8BPS owl",              # similar name, different folder
        "Current/same_copy.psd": b"8BPS shared content",  # same bytes as Old/same.psd
        "Current/bird.psd": b"8BPS bird",
        "Current/ref.png": b"\x89PNG ref",
    }
    for rel, data in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    watched = [str(home / "Old"), str(home / "Old" / "Current"), str(home / "Old2"), str(home / "Current")]
    cfg = Config(watched_folders=watched, vault_dir=str(tmp_path / "Vault"), include_images=True)
    v = Vault(cfg)
    for p in home.rglob("*"):
        if p.is_file():
            v.backup_file(p)
    return cfg, v, home


def names(v):
    return sorted(Path(f.path).name for f in v.list_files(False))


def test_removing_a_folders_backups_never_touches_her_files(tmp_path):
    cfg, v, home = setup(tmp_path)
    before = snapshot(home)
    cfg.watched_folders.remove(str(home / "Old"))
    files, size = v.folder_backup_size(home / "Old", cfg.watched_folders)
    assert files == 3 and size > 0  # dragon, cat, same: fox is still protected via Old\Current
    v.remove_folder_backups(home / "Old", cfg.watched_folders)
    assert snapshot(home) == before  # every one of her files still there, byte for byte
    assert names(v) == ["bird.psd", "fox.psd", "owl.psd", "ref.png", "same_copy.psd"]
    # the copy shared with a kept file is still restorable
    [row] = [f for f in v.list_files(False) if f.name == "same_copy.psd"]
    assert v.blob_path(row.latest_blob, ".psd").exists()
    v.close()


def test_removing_image_backups_never_touches_her_files(tmp_path):
    cfg, v, home = setup(tmp_path)
    before = snapshot(home)
    v.remove_backups_of_other_types(set(DRAWING_EXTENSIONS))
    assert snapshot(home) == before
    assert "ref.png" not in names(v) and "dragon.psd" in names(v)
    v.close()


def test_cleanup_only_deletes_artsafe_copies(tmp_path):
    cfg, v, home = setup(tmp_path)
    # something that isn't an ArtSafe copy ended up in the backup folder
    stray = Path(cfg.vault_dir) / "blobs" / "ab" / "my drawing.psd"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"8BPS not ours")
    v.remove_folder_backups(home / "Old", [])
    v.remove_backups_of_other_types(set())
    v.prune()
    assert stray.exists()
    assert (home / "Old" / "dragon.psd").read_bytes() == b"8BPS dragon"
    v.close()


def test_backup_folder_inside_her_art_folder(tmp_path):
    art = tmp_path / "Art"
    art.mkdir()
    (art / "pic.psd").write_bytes(b"8BPS pic")
    cfg = Config(watched_folders=[str(art)], vault_dir=str(art / "ArtSafe Backups"))
    v = Vault(cfg)
    v.backup_file(art / "pic.psd")
    before = snapshot(art)
    v.remove_folder_backups(art, [])
    after = snapshot(art)
    assert (art / "pic.psd").read_bytes() == b"8BPS pic"
    assert {k: h for k, h in before.items() if not k.startswith("ArtSafe Backups")} == \
           {k: h for k, h in after.items() if not k.startswith("ArtSafe Backups")}
    v.close()
