import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def provision(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "first-officer/provision-andrea.py"
    spec = importlib.util.spec_from_file_location("fo_andrea_provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / ".ssh/openclaw-fo-andrea")
    return module, tmp_path


def test_reader_key_creation_can_be_retried(provision):
    module, _ = provision
    a = module.make_key()
    b = module.make_key()
    assert a == b
    assert module.PUBLIC_KEY.fullmatch(a["public"])
    assert (module.STATE / "id_ed25519").stat().st_mode & 0o077 == 0


def test_mac_setup_appends_only_its_restricted_key_and_preserves_existing_keys(provision):
    module, home = provision
    key_file = home / ".ssh/authorized_keys"
    key_file.parent.mkdir(mode=0o700)
    previous = b"# Keep the operator's SSH configuration.\n"
    key_file.write_bytes(previous)
    key_file.chmod(0o600)
    key = module.make_key()["public"]
    entry = f'restrict,command="fixture-read-only-command" {key} {module.KEY_LABEL}'.encode()
    module.add_restriction(entry)
    module.add_restriction(entry)
    assert key_file.read_bytes() == previous + entry + b"\n"

    with pytest.raises(ValueError, match="needs operator review"):
        module.add_restriction(entry.replace(b"fixture-read-only-command", b"different-command"))
    assert key_file.read_bytes() == previous + entry + b"\n"
