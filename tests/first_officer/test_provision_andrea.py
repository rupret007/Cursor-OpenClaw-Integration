import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
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
    monkeypatch.setattr(module, "LOCAL_STATE", tmp_path / ".ssh/openclaw-fo-andrea-nuc")
    return module, tmp_path


def test_reader_key_creation_can_be_retried(provision):
    module, _ = provision
    a = module.make_key()
    b = module.make_key()
    assert a == b
    assert module.PUBLIC_KEY.fullmatch(a["public"])
    assert (module.STATE / "id_ed25519").stat().st_mode & 0o077 == 0


def test_local_client_uses_its_own_key_and_talks_only_to_the_mac(provision, monkeypatch, capsys):
    module, home = provision
    incumbent = module.make_key()["public"]
    calls = []

    def remote(host, action, payload, _source):
        calls.append((host, action))
        assert (host, action) == ("operator@fixture", "remote-mac")
        assert payload["local_client"] is True
        assert payload["public"] != incumbent
        entry = f'restrict,command="fixture-local-read-only" {payload["public"]} {module.LOCAL_KEY_LABEL}'.encode()
        module.add_restriction(entry, label=module.LOCAL_KEY_LABEL)
        return {"pin": incumbent}

    def verified(payload):
        assert payload["local_client"] is True
        assert (module.LOCAL_STATE / "id_ed25519").is_file()
        return {"verified": True}

    entry = f'restrict,command="fixture-incumbent-read-only" {incumbent} {module.KEY_LABEL}'.encode()
    module.add_restriction(entry)
    monkeypatch.setattr(module, "run_on_host", remote)
    monkeypatch.setattr(module, "verify_from_client", verified)
    monkeypatch.setattr(sys, "argv", ["provision-andrea.py", "--mac-host", "fixture", "--mac-user",
                                     "operator", "--local-client"])
    assert module.main() == 0
    assert calls == [("operator@fixture", "remote-mac")]
    keys = (home / ".ssh/authorized_keys").read_bytes().splitlines()
    assert keys[0] == entry
    assert keys[1].endswith(b" " + module.LOCAL_KEY_LABEL.encode())
    assert "Andrea reader verified from the local Captain host." in capsys.readouterr().out


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


@pytest.mark.parametrize("switch_error", [OSError, KeyboardInterrupt])
def test_repeated_reader_refresh_restores_the_immediately_previous_restriction(provision, monkeypatch, switch_error):
    module, home = provision
    root = home / ".local/libexec/openclaw-firstofficer-andrea"
    key = module.make_key()["public"]

    def entry(digest):
        script = root / (digest * 64) / "andrea-mac.mjs"
        script.parent.mkdir(parents=True)
        script.write_text("// local synthetic reader")
        return f'restrict,command="/usr/bin/node {script}" {key} {module.KEY_LABEL}'.encode()

    legacy, old, first, second = entry("a"), entry("b"), entry("c"), entry("d")
    key_file = home / ".ssh/authorized_keys"
    unrelated = b"# Unrelated entry is preserved.\n"
    key_file.write_bytes(unrelated)
    key_file.chmod(0o600)
    module.add_restriction(old)
    module.write_once(module.STATE / "reader.previous", legacy)
    module.add_restriction(first, "refresh")
    assert key_file.read_bytes() == unrelated + first + b"\n"
    first_backup = module.reader_backup_path(first)
    second_backup = module.reader_backup_path(second)
    assert module.private_file(first_backup) == old

    second_backup.write_bytes(first)
    second_backup.chmod(0o644)
    with pytest.raises(ValueError, match="Reader file is not private"):
        module.add_restriction(second, "refresh")
    second_backup.unlink()
    assert key_file.read_bytes() == unrelated + first + b"\n"
    assert module.private_file(first_backup) == old

    replace = module.os.replace

    def reject_authorized_keys_update(source, destination):
        if destination == key_file:
            raise switch_error("synthetic authorized keys replacement failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", reject_authorized_keys_update)
        with pytest.raises(switch_error, match="synthetic authorized keys replacement failure"):
            module.add_restriction(second, "refresh")
    assert key_file.read_bytes() == unrelated + first + b"\n"
    assert module.private_file(first_backup) == old

    module.add_restriction(second, "refresh")
    module.add_restriction(second, "refresh")
    assert key_file.read_bytes() == unrelated + second + b"\n"
    assert module.private_file(second_backup) == first
    for prior in (first, old, legacy, legacy):
        assert module.restore_mac() == {"restored": True}
        assert key_file.read_bytes() == unrelated + prior + b"\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="The reader verification uses Node")
def test_bobserver_refresh_verifies_singular_and_plural_activity_and_rejects_empty_history(provision):
    module, _ = provision
    public = module.make_key()["public"]
    payload = {"host": "fixture", "user": "operator", "pin": public, "require_recent": True}
    for count, noun, activity in (
        (1, "conversation", "Topics · scheduling."),
        (2, "conversations", "The recent previews did not give Andrea a clear topic or request."),
    ):
        summary = f"Andrea · fixture history.\n{activity}\n2 incoming, 1 outgoing across {count} recent {noun}."
        source = "export const sshAndreaSummary = () => " + json.dumps(summary)
        assert module.verify_from_client({**payload, "client": source}) == {"verified": True}
    for summary in (
        "Andrea returned no recent previews.",
        "Andrea · fixture history.\n2 incoming, 1 outgoing across 2 recent conversations.",
    ):
        source = "export const sshAndreaSummary = () => " + json.dumps(summary)
        with pytest.raises(subprocess.CalledProcessError):
            module.verify_from_client({**payload, "client": source})


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


def test_remote_bootstrap_returns_a_safe_result_if_the_helper_crashes(provision):
    module, _ = provision
    call = subprocess.run([sys.executable, "-c", module.REMOTE_BOOTSTRAP, "remote-mac"],
                          input=json.dumps({"payload": {}, "source": "raise RuntimeError('private fixture data')"}),
                          text=True, capture_output=True, check=False)
    assert call.returncode == 1
    assert json.loads(call.stdout) == {"problem": "The remote Python helper could not run."}
    assert call.stderr == ""
