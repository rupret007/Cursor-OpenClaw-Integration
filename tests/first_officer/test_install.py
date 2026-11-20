import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    installer = importlib.import_module("install")
    store = tmp_path / "releases"
    monkeypatch.setattr(installer, "STORE", store)
    release = hashlib.sha256(b"").hexdigest()
    bundle = store / f"first-officer-{release}"
    bundle.mkdir(parents=True)
    (bundle / "SHA256SUMS").write_bytes(b"")
    base = store / f"first-officer-{installer.BASELINE}"
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({
        "gateway": {"port": 18101},
        "plugins": {
            "load": {"paths": [str(base / "first-officer-exec-guard.mjs"),
                               str(base / "round6/runtime/first-officer-conductor.mjs")]},
            "entries": {installer.PLUGIN: {"enabled": True, "hooks": {"allowConversationAccess": True}}},
        },
        "models": {"providers": {"litellm": {"models": [{"id": installer.FAST_MODEL.split("/")[1]}]}}},
        "tools": {"deny": ["write", "message"]},
    }), encoding="utf-8")
    config.chmod(0o600)
    pin = tmp_path / "pin.conf"
    pin.write_text("[Service]\n" + installer.pin_line(installer.BASELINE) + "\n", encoding="utf-8")
    calls = []

    def service(*args, capture=False):
        calls.append(args[0])
        return str(pin) if capture else None

    monkeypatch.setattr(installer, "systemctl", service)
    monkeypatch.setattr(installer, "verify_config", lambda *args: calls.append("verify"))
    monkeypatch.setattr(installer, "wait_ready", lambda *args: calls.append("ready"))
    backup = tmp_path / "backup"
    installer.prepare(release, backup, config)
    for member in backup.iterdir():
        member.chmod(0o600)
    return installer, release, config, pin, backup, calls


def test_install_switch_and_rollback_preserve_prior_config_and_pin(controller):
    installer, release, config, pin, backup, calls = controller
    original = config.read_bytes(), pin.read_bytes()
    assert "stop" not in calls
    installer.switch(backup, config, restore=False)
    activated = json.loads(config.read_bytes())
    assert activated["tools"] == {"deny": ["write", "message"]}
    auth = activated["plugins"]["entries"][installer.PLUGIN]
    assert auth["hooks"] == {"allowConversationAccess": True}
    assert auth["llm"]["allowedCompletionModels"] == [installer.FAST_MODEL]
    assert installer.pin_line(release) in pin.read_text(encoding="utf-8")
    assert calls[-5:] == ["verify", "stop", "daemon-reload", "start", "ready"]
    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original


def test_gateway_start_failure_restores_prior_bytes_and_reports_failure(controller, monkeypatch):
    installer, _, config, pin, backup, calls = controller
    original = config.read_bytes(), pin.read_bytes()

    def wait(payload):
        calls.append("ready")
        if payload != original[0]:
            raise ValueError("synthetic readiness failure")

    monkeypatch.setattr(installer, "wait_ready", wait)
    with pytest.raises(ValueError, match="prior configuration and service were restored"):
        installer.switch(backup, config, restore=False)
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert calls.count("ready") == 2


def test_switch_refuses_a_changed_config_without_stopping_the_service(controller):
    installer, _, config, _, backup, calls = controller
    config.write_bytes(config.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed since preparation"):
        installer.switch(backup, config, restore=False)
    assert "stop" not in calls
