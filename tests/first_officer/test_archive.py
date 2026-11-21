import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_archive", ROOT / "first-officer/archive.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def _tree(tmp_path, secret=False):
    root = tmp_path / "src"
    for name in archive.DOCS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n", encoding="utf-8")
    scan = root / "first-officer" / "privacy" / "scan.py"
    scan.parent.mkdir(parents=True)
    scan.write_text("def scan():\n    return 'clean'\n", encoding="utf-8")
    note = root / "tests" / "first_officer" / "test_note.py"
    note.parent.mkdir(parents=True)
    note.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    if secret:
        (root / "first-officer" / "leaked.txt").write_text("sk-" + "live-othersecret\n", encoding="utf-8")
    return root


def test_pack_and_verify_round_trip_without_touching_the_gateway(tmp_path, capsys):
    first = archive.pack(ROOT, tmp_path / "one")
    second = archive.pack(ROOT, tmp_path / "two")
    assert first["digest"] == second["digest"] and len(first["digest"]) == 64
    checked = archive.verify(first["archive"])
    assert checked["digest"] == first["digest"] and checked["files"] == first["files"]
    mode = first["archive"].stat().st_mode
    assert stat.S_IMODE(mode) == 0o600 and first["archive"].stat().st_uid == os.getuid()
    with tarfile.open(first["archive"], "r:") as bundle:
        names = bundle.getnames()
    assert f"first-officer-review-{first['digest']}/CAPTAIN.md" in names
    assert f"first-officer-review-{first['digest']}/first-officer/privacy/scan.py" in names
    assert archive.main(["pack", str(tmp_path / "cli")]) == 0
    printed = capsys.readouterr().out
    assert first["digest"] in printed and "The gateway was not touched." in printed


def test_a_secret_is_not_packed_and_live_paths_are_refused(tmp_path):
    root = _tree(tmp_path, secret=True)
    with pytest.raises(ValueError, match="secret or personal data"):
        archive.pack(root, tmp_path / "out")
    assert list((tmp_path / "out").glob("*")) == []
    with pytest.raises(ValueError, match="live gateway"):
        archive.pack(ROOT, Path("/opt/openclaw-fo/out"))
    with pytest.raises(ValueError, match="outside the checkout"):
        archive.pack(ROOT, ROOT / "packed")


def test_verify_rejects_a_path_escape_and_a_changed_file(tmp_path):
    root = _tree(tmp_path)
    packed = archive.pack(root, tmp_path / "out")
    with tarfile.open(packed["archive"], "r:") as bundle:
        members = {member.name: bundle.extractfile(member).read() for member in bundle.getmembers()}
    escaped = tmp_path / "escaped.tar"
    with tarfile.open(escaped, "w") as bundle:
        info = tarfile.TarInfo("../outside.txt")
        payload = b"nope\n"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    os.chmod(escaped, 0o600)
    with pytest.raises(ValueError, match="refused a path"):
        archive.verify(escaped)
    changed = tmp_path / "changed.tar"
    folder = f"first-officer-review-{packed['digest']}"
    with tarfile.open(changed, "w") as bundle:
        for name, payload in members.items():
            body = b"changed\n" if name.endswith("/CAPTAIN.md") else payload
            info = tarfile.TarInfo(name)
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))
    os.chmod(changed, 0o600)
    with pytest.raises(ValueError, match="checksums"):
        archive.verify(changed)
    assert folder.startswith("first-officer-review-")
