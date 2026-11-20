import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_reader_refresh_and_restore_replace_only_the_dedicated_line(provision):
    module, home = provision
    root = home / ".local/libexec/openclaw-firstofficer-andrea"
    key = module.make_key()["public"]

    def entry(digest):
        script = root / (digest * 64) / "andrea-mac.mjs"
        script.parent.mkdir(parents=True)
        script.write_text("// local synthetic reader")
        return f'restrict,command="/usr/bin/node {script}" {key} {module.KEY_LABEL}'.encode()

    old, new = entry("a"), entry("b")
    key_file = home / ".ssh/authorized_keys"
    unrelated = b"# Unrelated entry is preserved.\n"
    key_file.write_bytes(unrelated)
    key_file.chmod(0o600)
    module.add_restriction(old)
    module.add_restriction(new, "refresh")
    module.add_restriction(new, "refresh")
    assert key_file.read_bytes() == unrelated + new + b"\n"
    assert module.restore_mac() == {"restored": True}
    assert key_file.read_bytes() == unrelated + old + b"\n"


@pytest.mark.parametrize(
    ("status", "stdout", "expected"),
    [
        (255, "", "ssh-session"),
        (127, "", "remote-python"),
        (1, '{"diagnostic":"api-authentication"}', "api-authentication"),
        (1, '{"diagnostic":"private fixture data"}', "remote-runtime"),
        (0, '{"output":"private fixture data"}', "invalid-counts"),
    ],
)
def test_diagnostic_exposes_only_known_failure_codes(provision, monkeypatch, status, stdout, expected):
    module, _ = provision
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=status, stdout=stdout))
    with pytest.raises(module.DiagnosticError) as caught:
        module.run_on_host("fixture", "remote-diagnose", {}, "")
    assert caught.value.code == expected
    assert "private fixture data" not in str(caught.value)


def test_diagnostic_resolves_mac_style_temp_directory_symlink(provision, monkeypatch):
    module, home = provision
    canonical = home / "physical"
    canonical.mkdir()
    alias = home / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(module.tempfile, "tempdir", str(alias))
    monkeypatch.setattr(module, "mac_node", lambda: "/fixture/node")

    def invoke(args, **kwargs):
        entry = Path(args[1])
        assert entry == entry.resolve(strict=True)
        assert entry.parent.parent == canonical
        return SimpleNamespace(returncode=1, stdout='{"problem":"settings-unavailable"}')

    monkeypatch.setattr(module.subprocess, "run", invoke)
    payload = {"files": dict.fromkeys(module.MODULES, "// fixture")}
    with pytest.raises(module.DiagnosticError) as caught:
        module.diagnose_mac(payload)
    assert caught.value.code == "settings-unavailable"


def test_only_allowed_sync_metadata_is_returned(provision):
    module, _ = provision
    status = {"channel": "connected", "scope": "allowlist", "transport": "reachable",
              "webhook": "registered", "shadowPoll": "recent", "lastInbound": "older"}
    assert module.validate_sync(status) == status
    with pytest.raises(module.DiagnosticError):
        module.validate_sync({**status, "channel": "private fixture detail"})


@pytest.mark.parametrize(("stderr", "detail"), [
    ("private fixture identity: Permission denied (publickey,password).", "SSH rejected the login."),
    ("Host key verification failed. private fixture identity", "SSH could not verify the saved host key."),
    ("ssh: connect to host private fixture identity: Connection refused", "SSH could not connect to the host."),
])
def test_refresh_distinguishes_ssh_failure_without_dumping_stderr(provision, monkeypatch, stderr, detail):
    module, _ = provision
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=255, stdout="", stderr=stderr))
    with pytest.raises(ValueError) as caught:
        module.run_on_host("fixture", "remote-mac", {}, "")
    assert str(caught.value) == detail
