import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

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
        "models": {"providers": {"litellm": {"apiKey": "${LEGACY_MODEL_KEY}",
                                             "models": [{"id": installer.FAST_MODEL.split("/")[1]}]}}},
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
    assert activated["models"]["providers"]["litellm"]["apiKey"] == "${OPENCLAW_LITELLM_API_KEY}"
    auth = activated["plugins"]["entries"][installer.PLUGIN]
    assert auth["hooks"] == {"allowConversationAccess": True}
    assert auth["llm"]["allowedCompletionModels"] == [installer.FAST_MODEL]
    assert installer.pin_line(release) in pin.read_text(encoding="utf-8")
    assert calls[-5:] == ["verify", "stop", "daemon-reload", "start", "ready"]
    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original


def test_legacy_rollback_accepts_a_plan_without_the_lane_key_marker(controller):
    installer, release, config, pin, backup, _ = controller
    plan_path = backup / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan.pop("budgeted_litellm")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    prior = config.read_bytes()
    old_candidate = installer.candidate_config(prior, release, budgeted_litellm=False)
    (backup / "candidate-openclaw.json").write_bytes(old_candidate)
    config.write_bytes(old_candidate)
    pin.write_bytes((backup / "candidate-pin.conf").read_bytes())
    installer.switch(backup, config, restore=True)
    assert config.read_bytes() == prior


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


def test_root_owned_pin_is_prepared_and_preserved_through_switch_and_rollback(controller, monkeypatch, tmp_path):
    installer, release, config, pin, _, calls = controller
    assert os.getuid() != 0
    pin.chmod(0o644)
    original = config.read_bytes(), pin.read_bytes()
    real_lstat = Path.lstat
    root_group = 0

    def simulated_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if path != pin:
            return info
        return SimpleNamespace(st_uid=0, st_gid=root_group, st_mode=info.st_mode,
                               st_nlink=info.st_nlink, st_size=info.st_size)

    monkeypatch.setattr(Path, "lstat", simulated_lstat)
    backup = tmp_path / "root-pin-backup"
    installer.prepare(release, backup, config)
    for member in backup.iterdir():
        member.chmod(0o600)
    assert json.loads((backup / "plan.json").read_text())["pin_identity"] == [0, root_group, 0o644]

    def denied():
        raise ValueError("Authorization denied")

    monkeypatch.setattr(installer, "authorize_root_pin", denied)
    with pytest.raises(ValueError, match="Authorization denied"):
        installer.switch(backup, config, restore=False)
    assert "stop" not in calls
    assert (config.read_bytes(), pin.read_bytes()) == original

    actions = []
    temporary = pin.parent / ".fo-pin.ABCD1234"

    def privileged(binary, *args, capture=False):
        actions.append(Path(binary).name)
        if binary.endswith("/mktemp"):
            assert Path(args[0]).parent == pin.parent
            assert capture
            temporary.touch(exist_ok=False)
            return str(temporary) + "\n"
        if binary.endswith("/install"):
            assert args[:8] == ("-o", "0", "-g", "0", "-m", "644", "-T", "--")
            temporary.write_bytes(Path(args[-2]).read_bytes())
            temporary.chmod(0o644)
            assert Path(args[-1]) == temporary
        if binary.endswith("/mv"):
            assert args == ("-fT", "--", str(temporary), str(pin))
            temporary.replace(pin)

    monkeypatch.setattr(installer, "authorize_root_pin", lambda: calls.append("sudo-authorized"))
    monkeypatch.setattr(installer, "sudo_pin", privileged)
    installer.switch(backup, config, restore=False)
    assert calls.index("sudo-authorized") < calls.index("stop")
    assert installer.pin_line(release) in pin.read_text()
    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert installer.pin_file(pin)[1] == [0, root_group, 0o644]
    assert actions == ["mktemp", "install", "mv"] * 2


def test_schema_validation_uses_the_node_launcher_from_systemd(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    installer = importlib.import_module("install")
    config = tmp_path / "candidate-openclaw.json"
    release = hashlib.sha256(b"candidate").hexdigest()
    calls = []
    launcher = ["/usr/bin/node", "--enable-source-maps", "/opt/openclaw/dist/index.js"]
    monkeypatch.setattr(installer, "systemctl", lambda *args, **kwargs:
                        "{ path=/usr/bin/node ; argv[]=" + " ".join(launcher) + " gateway ; ignore_errors=no ; }")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='{"valid":true}', returncode=0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.verify_config(config, release)
    assert Path(calls[0][0][3]).name == "verify-install.py"
    assert calls[0][1]["check"] is True
    assert calls[1][0] == [*launcher, "config", "validate", "--json"]
    assert calls[1][1]["env"]["OPENCLAW_CONFIG_PATH"] == str(config)


@pytest.mark.parametrize("prior_name", ["SEGMENT1", "CAPTAIN"])
def test_upgrade_from_installed_release_restores_its_exact_bytes(controller, tmp_path, prior_name):
    installer, release, config, pin, _, _ = controller
    prior = getattr(importlib.import_module("stage"), prior_name)
    config.write_bytes(installer.candidate_config(config.read_bytes(), prior))
    pin.write_text("[Service]\n" + installer.pin_line(prior) + "\n")
    old = config.read_bytes(), pin.read_bytes()
    backup = tmp_path / "captain"
    installer.prepare(release, backup, config, prior)
    for member in backup.iterdir():
        member.chmod(0o600)
    installer.switch(backup, config, restore=False)
    assert installer.pin_line(release) in pin.read_text()
    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == old


def test_upgrade_from_captain_rejects_custom_model_policy(controller):
    installer, release, config, _, _, _ = controller
    prior = importlib.import_module("stage").CAPTAIN
    active = json.loads(installer.candidate_config(config.read_bytes(), prior))
    entry = active["plugins"]["entries"][installer.PLUGIN]
    entry["llm"]["allowModelOverride"] = False
    with pytest.raises(ValueError, match="custom model policy"):
        installer.candidate_config(json.dumps(active), release, prior)


def test_stage_from_captain_preserves_wiring_and_refreshes_overlay(controller, tmp_path, monkeypatch):
    stage = importlib.import_module("stage")
    baseline = tmp_path / "captain-base"
    runtime = baseline / stage.RUNTIME
    segment = runtime / "segment1"
    segment.mkdir(parents=True)
    conductor = runtime / "first-officer-conductor.mjs"
    conductor.write_bytes(b"already wired")
    router = baseline / conductor.name
    router.write_text(
        "import { ownerFallbacks } from './round6/runtime/model-failover.mjs';\n"
        "const selection = await routeOwnerModel(event, api.config, routeFor, ownerFallbacks);\n",
        encoding="utf-8",
    )
    old_tool = segment / "tools.mjs"
    old_tool.write_bytes(b"old overlay")
    manifest = b"".join(
        f"{stage.digest(path.read_bytes())}  {path.relative_to(baseline).as_posix()}\n".encode()
        for path in (conductor, router, old_tool)
    )
    (baseline / stage.INVENTORY).write_bytes(manifest)
    captain = stage.digest(manifest)
    monkeypatch.setattr(stage, "CAPTAIN", captain)
    monkeypatch.setattr(stage, "SUPPORTED_BASES", (stage.BASELINE, stage.SEGMENT1, captain))
    destination = tmp_path / "staging"
    destination.mkdir()

    release = stage.stage_release(baseline, destination)

    candidate = destination / f"first-officer-{release}"
    assert (candidate / stage.RUNTIME / conductor.name).read_bytes() == b"already wired"
    assert (candidate / router.name).read_text(encoding="utf-8") == (
        "const selection = routeOwnerModel(event, api.config, routeFor);\n"
    )
    assert (candidate / stage.RUNTIME / "segment1" / old_tool.name).read_text(encoding="utf-8") == (
        stage.OVERLAY / old_tool.name
    ).read_text(encoding="utf-8")
    assert old_tool.read_bytes() == b"old overlay"
    stage.verify_inventory(candidate, release)
