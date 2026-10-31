import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from artsafe import updater


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def pub_hex(k):
    return k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def sign(k, path: Path) -> bytes:
    return k.sign(hashlib.sha256(path.read_bytes()).digest())


def make_zip(path: Path, files: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


@pytest.fixture
def release(tmp_path, key, monkeypatch):
    """A fake GitHub release served from local files."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(updater, "PUBLIC_KEY_HEX", pub_hex(key))
    src = tmp_path / "server"
    src.mkdir()
    z = src / "ArtSafe-9.9.9.zip"
    make_zip(z, {"ArtSafe/ArtSafe.exe": b"new exe", "ArtSafe/_internal/x.dll": b"lib"})
    (src / "ArtSafe-9.9.9.zip.sig").write_bytes(sign(key, z))
    monkeypatch.setattr(updater, "download", lambda url, dest: shutil.copy(url, dest))
    monkeypatch.setattr(updater, "verify", lambda p, s, k=None: updater.__dict__["_real_verify"](p, s, pub_hex(key)))
    return {"version": "9.9.9", "zip": str(z), "sig": str(src / "ArtSafe-9.9.9.zip.sig"), "notes": ""}


updater._real_verify = updater.verify


@pytest.mark.parametrize("remote,local,newer", [
    ("1.0.1", "1.0.0", True), ("v1.2.0", "1.10.0", False), ("2.0", "1.9.9", True),
    ("1.0.0", "1.0.0", False), ("1.0.0-beta", "1.0.0", False),
])
def test_version_compare(remote, local, newer):
    assert updater.is_newer(remote, local) is newer


def test_good_update_is_unpacked(release):
    folder = updater.prepare(release)
    assert (folder / "ArtSafe.exe").read_bytes() == b"new exe"


def test_tampered_update_is_refused(release):
    z = Path(release["zip"])
    with zipfile.ZipFile(z, "a") as zz:
        zz.writestr("ArtSafe/evil.dll", b"bad")
    with pytest.raises(ValueError, match="signature"):
        updater.prepare(release)
    assert not updater.updates_dir().exists()


def test_update_signed_by_someone_else_is_refused(release, key):
    other = Ed25519PrivateKey.generate()
    Path(release["sig"]).write_bytes(sign(other, Path(release["zip"])))
    with pytest.raises(ValueError, match="signature"):
        updater.prepare(release)


def test_zip_with_paths_outside_folder_is_refused(release, key):
    z = Path(release["zip"])
    make_zip(z, {"ArtSafe/ArtSafe.exe": b"x", "../../outside.txt": b"bad"})
    Path(release["sig"]).write_bytes(sign(key, z))
    with pytest.raises(ValueError, match="unsafe"):
        updater.prepare(release)


def test_real_public_key_is_valid():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(updater.PUBLIC_KEY_HEX))
