import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
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
    monkeypatch.setattr(installer, "verify_config", lambda *args, **kwargs: calls.append("verify"))
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


def test_activation_and_rollback_recover_every_intermediate_pair(controller):
    """A crash between the pin and config writes must still roll back or finish."""
    installer, _release, config, pin, backup, calls = controller
    original = (
        (backup / "original-openclaw.json").read_bytes(),
        (backup / "original-pin.conf").read_bytes(),
    )
    candidate = (
        (backup / "candidate-openclaw.json").read_bytes(),
        (backup / "candidate-pin.conf").read_bytes(),
    )
    pairs = {"original": original, "candidate": candidate}
    for restore, target in ((True, "original"), (False, "candidate")):
        for config_state in ("original", "candidate"):
            for pin_state in ("original", "candidate"):
                config.write_bytes(pairs[config_state][0])
                pin.write_bytes(pairs[pin_state][1])
                before = len(calls)
                installer.switch(backup, config, restore=restore)
                assert config.read_bytes() == pairs[target][0]
                assert pin.read_bytes() == pairs[target][1]
                assert calls[before:][:2] == ["show", "verify"]
                assert "stop" in calls[before:]
                assert calls[before:][-3:] == ["daemon-reload", "start", "ready"]
    config.write_bytes(original[0] + b"\n")
    pin.write_bytes(original[1])
    before = len(calls)
    with pytest.raises(ValueError, match="changed since preparation"):
        installer.switch(backup, config, restore=True)
    assert "stop" not in calls[before:]
    assert config.read_bytes() == original[0] + b"\n"
    assert pin.read_bytes() == original[1]


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


def test_readiness_failure_keeps_the_complete_pair_observed_at_entry(controller, monkeypatch):
    """A failed readiness check restores the saved prior pair.

    Rollback and activation both do this for a mixed pair and for either
    complete pair. A repeated rollback that already has the original pair
    stays on that pair.
    """
    installer, _, config, pin, backup, calls = controller
    original = (
        (backup / "original-openclaw.json").read_bytes(),
        (backup / "original-pin.conf").read_bytes(),
    )
    candidate = (
        (backup / "candidate-openclaw.json").read_bytes(),
        (backup / "candidate-pin.conf").read_bytes(),
    )
    pairs = {"original": original, "candidate": candidate}
    installer.switch(backup, config, restore=False)
    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original
    attempts = {"n": 0}

    def wait(_payload):
        attempts["n"] += 1
        calls.append("ready")
        if attempts["n"] % 2 == 1:
            raise ValueError("synthetic readiness failure")

    monkeypatch.setattr(installer, "wait_ready", wait)
    with pytest.raises(ValueError, match="prior configuration and service were restored"):
        installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert (config.read_bytes(), pin.read_bytes()) != candidate

    config.write_bytes(original[0])
    pin.write_bytes(candidate[1])
    attempts["n"] = 0
    with pytest.raises(ValueError, match="prior configuration and service were restored"):
        installer.switch(backup, config, restore=True)
    failed_mixed_rollback_reactivated_candidate = (config.read_bytes(), pin.read_bytes()) == candidate
    assert failed_mixed_rollback_reactivated_candidate is False
    assert (config.read_bytes(), pin.read_bytes()) == original

    for restore in (True, False):
        for config_state in ("original", "candidate"):
            for pin_state in ("original", "candidate"):
                observed = (pairs[config_state][0], pairs[pin_state][1])
                config.write_bytes(observed[0])
                pin.write_bytes(observed[1])
                attempts["n"] = 0
                with pytest.raises(ValueError, match="prior configuration and service were restored"):
                    installer.switch(backup, config, restore=restore)
                assert (config.read_bytes(), pin.read_bytes()) == original
                assert (config.read_bytes(), pin.read_bytes()) != candidate


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


def _write_release(root, files):
    entries = []
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
    payload = ("\n".join(entries) + "\n").encode()
    (root / "SHA256SUMS").write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("recheck", ["switch", "verify-candidate"])
@pytest.mark.parametrize("live_pair", ["candidate", "candidate-config", "candidate-pin", "journal"])
def test_prepare_verifies_the_unpacked_candidate_and_rollback_restores_the_prior_bundle(
        tmp_path, monkeypatch, recheck, live_pair):
    """Prepare checks the unpacked tree before it is published, and rollback puts the prior bytes back."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    installer = importlib.import_module("install")
    store = tmp_path / "releases"
    store.mkdir()
    monkeypatch.setattr(installer, "STORE", store)
    prior_files = {
        "first-officer-exec-guard.mjs": b"prior-guard\n",
        "round6/runtime/first-officer-conductor.mjs": b"prior-conductor\n",
    }
    prior = store / f"first-officer-{installer.BASELINE}"
    _write_release(prior, prior_files)
    prior_bytes = {name: (prior / name).read_bytes() for name in prior_files}
    unpacked = tmp_path / "unpacked"
    release = _write_release(unpacked, {
        "first-officer-exec-guard.mjs": b"candidate-guard\n",
        "round6/runtime/first-officer-conductor.mjs": b"candidate-conductor\n",
    })
    assert release not in installer.SUPPORTED_BASES
    config = tmp_path / "openclaw.json"
    base = store / f"first-officer-{installer.BASELINE}"
    config.write_text(json.dumps({
        "gateway": {"port": 18101},
        "plugins": {
            "load": {"paths": [str(base / "first-officer-exec-guard.mjs"),
                               str(base / "round6/runtime/first-officer-conductor.mjs")]},
            "entries": {installer.PLUGIN: {"enabled": True}},
        },
        "models": {"providers": {"litellm": {"models": [{"id": installer.FAST_MODEL.split("/")[1]}]}}},
    }), encoding="utf-8")
    config.chmod(0o600)
    pin = tmp_path / "pin.conf"
    pin.write_text("[Service]\n" + installer.pin_line(installer.BASELINE) + "\n", encoding="utf-8")
    launcher = tmp_path / "openclaw"
    launcher.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'valid': True}))\n", encoding="utf-8")
    launcher.chmod(0o755)
    verifier = store / "verify-install.py"
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "log = Path(__file__).with_name('verifier-args.log')\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if len(sys.argv) != 4:\n"
        "    raise SystemExit(2)\n"
        "config, release, rollback = sys.argv[1:]\n"
        f"if rollback != {installer.ROLLBACK_SLOT!r}:\n"
        "    raise SystemExit(3)\n"
        "paths = json.loads(Path(config).read_text(encoding='utf-8'))['plugins']['load']['paths']\n"
        "marker = f'/first-officer-{release}/'\n"
        "for raw in paths:\n"
        "    if marker not in raw or not Path(raw).is_file():\n"
        "        raise SystemExit(4)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    recorded = []
    real_run = installer.subprocess.run

    def wrapped(args, **kwargs):
        recorded.append([str(part) for part in args])
        return real_run(args, **kwargs)

    stops = []

    def service(*args, capture=False):
        if args and args[0] == "stop":
            stops.append(len(recorded))
        if args[:3] == ("show", "-p", "ExecStart"):
            return "{ argv[]=" + str(launcher) + " gateway ; }"
        if capture:
            return str(pin)
        return None

    def verifier_calls(calls):
        return [args for args in calls if any(part.endswith("verify-install.py") for part in args)]

    monkeypatch.setattr(installer.subprocess, "run", wrapped)
    monkeypatch.setattr(installer, "systemctl", service)
    monkeypatch.setattr(installer, "wait_ready", lambda *_args: None)
    missing = tmp_path / "missing-plugin"
    partial = _write_release(missing, {
        "round6/runtime/first-officer-conductor.mjs": b"candidate-conductor\n",
    })
    with pytest.raises(ValueError, match="missing a plugin"):
        installer.prepare(partial, tmp_path / "bad-switch", config, installer.BASELINE, missing)
    original = config.read_bytes(), pin.read_bytes()
    backup = tmp_path / "switch"
    before = len(recorded)
    installer.prepare(release, backup, config, installer.BASELINE, unpacked)
    for member in backup.iterdir():
        member.chmod(0o600)
    published = store / f"first-officer-{release}"
    assert not published.exists()
    prepared = verifier_calls(recorded[before:])
    assert prepared == [installer.installed_verifier(backup / "original-openclaw.json", installer.BASELINE)]
    logged = (store / "verifier-args.log").read_text(encoding="utf-8")
    assert release not in logged and str(unpacked) not in logged
    assert all(len(json.loads(line)) == 3 for line in logged.splitlines())
    shutil.copytree(unpacked, published)
    (published / "first-officer-exec-guard.mjs").unlink()
    with pytest.raises(ValueError, match="rejected"):
        installer.verify_installed_candidate(backup, config)
    assert not published.exists()
    assert stops == []
    assert config.read_bytes() == original[0]
    shutil.copytree(unpacked, published)
    (published / "first-officer-exec-guard.mjs").unlink()
    with pytest.raises(ValueError, match="rejected"):
        installer.switch(backup, config, restore=False)
    assert stops == []
    assert not published.exists()
    assert config.read_bytes() == original[0]
    shutil.copytree(unpacked, published)
    before_verify = len(recorded)
    installer.verify_installed_candidate(backup, config)
    verified = verifier_calls(recorded[before_verify:])
    assert verified == [installer.installed_verifier(backup / "candidate-openclaw.json", release)]
    assert str(unpacked) not in " ".join(verified[0])
    assert published.is_dir()
    before_switch = len(recorded)
    installer.switch(backup, config, restore=False)
    switched = verifier_calls(recorded[before_switch:stops[0]])
    assert switched == [installer.installed_verifier(backup / "candidate-openclaw.json", release)]
    assert str(unpacked) not in " ".join(switched[0])
    activated = json.loads(config.read_bytes())
    assert str(published) in activated["plugins"]["load"]["paths"][0]

    if live_pair in ("candidate-config", "journal"):
        pin.write_bytes(original[1])
    if live_pair in ("candidate-pin", "journal"):
        config.write_bytes(original[0])
    if live_pair == "journal":
        installer._write_journal(backup, "original")
    live = config.read_bytes(), pin.read_bytes()
    stops_before_recheck = stops[:]
    plugin = published / "first-officer-exec-guard.mjs"
    plugin_bytes = plugin.read_bytes()
    plugin.unlink()
    with pytest.raises(ValueError, match="rejected"):
        if recheck == "switch":
            installer.switch(backup, config, restore=False)
        else:
            installer.verify_installed_candidate(backup, config)
    assert published.is_dir()
    assert (published / "round6/runtime/first-officer-conductor.mjs").is_file()
    assert stops == stops_before_recheck
    assert (config.read_bytes(), pin.read_bytes()) == live
    plugin.write_bytes(plugin_bytes)

    installer.switch(backup, config, restore=True)
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert {name: (prior / name).read_bytes() for name in prior_files} == prior_bytes
    assert (prior / "first-officer-exec-guard.mjs").read_bytes() == b"prior-guard\n"
    assert stat.S_ISREG((prior / "SHA256SUMS").stat().st_mode)
    guide = Path(__file__).resolve().parents[2] / "JEFF-ACTIONS.md"
    text = guide.read_text(encoding="utf-8")
    assert "python3 -I -B /opt/openclaw-fo/verify-install.py" in text
    assert installer.ROLLBACK_SLOT in text


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
    assert calls[0][0] == installer.installed_verifier(config, release)
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


def test_rollback_restores_the_prior_pair_when_the_production_seal_is_required(tmp_path, monkeypatch, capsys):
    """Documented rollback ignores a required seal. Activation still fails closed without one."""
    monkeypatch.delenv("FO_ACCEPTANCE_ROOT", raising=False)
    monkeypatch.delenv("FO_PRODUCTION_AUDIT_ANCHOR", raising=False)
    monkeypatch.delenv("FO_AUDIT_ANCHOR", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    installer = importlib.import_module("install")
    release_tool = importlib.import_module("release")
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(installer.Path, "home", lambda: home)
    store = tmp_path / "releases"
    store.mkdir()
    monkeypatch.setattr(installer, "STORE", store)
    prior_files = {
        "first-officer-exec-guard.mjs": b"prior-guard\n",
        "round6/runtime/first-officer-conductor.mjs": b"prior-conductor\n",
    }
    prior = store / f"first-officer-{installer.BASELINE}"
    _write_release(prior, prior_files)
    unpacked = tmp_path / "unpacked"
    release = _write_release(unpacked, {
        "first-officer-exec-guard.mjs": b"candidate-guard\n",
        "round6/runtime/first-officer-conductor.mjs": b"candidate-conductor\n",
    })
    config = home / ".openclaw" / "openclaw.json"
    base = store / f"first-officer-{installer.BASELINE}"
    config.write_text(json.dumps({
        "gateway": {"port": 18101},
        "plugins": {
            "load": {"paths": [str(base / "first-officer-exec-guard.mjs"),
                               str(base / "round6/runtime/first-officer-conductor.mjs")]},
            "entries": {installer.PLUGIN: {"enabled": True}},
        },
        "models": {"providers": {"litellm": {"models": [{"id": installer.FAST_MODEL.split("/")[1]}]}}},
    }), encoding="utf-8")
    config.chmod(0o600)
    pin = tmp_path / "pin.conf"
    pin.write_text("[Service]\n" + installer.pin_line(installer.BASELINE) + "\n", encoding="utf-8")
    pin.chmod(0o600)
    launcher = tmp_path / "openclaw"
    launcher.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'valid': True}))\n", encoding="utf-8")
    launcher.chmod(0o755)
    verifier = store / "verify-install.py"
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if len(sys.argv) != 4:\n"
        "    raise SystemExit(2)\n"
        "config, release, rollback = sys.argv[1:]\n"
        f"if rollback != {installer.ROLLBACK_SLOT!r}:\n"
        "    raise SystemExit(3)\n"
        "paths = json.loads(Path(config).read_text(encoding='utf-8'))['plugins']['load']['paths']\n"
        "marker = f'/first-officer-{release}/'\n"
        "for raw in paths:\n"
        "    if marker not in raw or not Path(raw).is_file():\n"
        "        raise SystemExit(4)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    recorded = []
    real_run = installer.subprocess.run

    def wrapped(args, **kwargs):
        recorded.append([str(part) for part in args])
        return real_run(args, **kwargs)

    stops = []
    started = []
    readiness = {"fail": 0}

    def service(*args, capture=False):
        if args and args[0] == "stop":
            stops.append(args[0])
        if args[:3] == ("show", "-p", "ExecStart"):
            return "{ argv[]=" + str(launcher) + " gateway ; }"
        if capture:
            return str(pin)
        return None

    def ready(payload):
        started.append(payload)
        if readiness["fail"]:
            readiness["fail"] -= 1
            raise ValueError("synthetic readiness failure")

    monkeypatch.setattr(installer.subprocess, "run", wrapped)
    monkeypatch.setattr(installer, "systemctl", service)
    monkeypatch.setattr(installer, "wait_ready", ready)
    backup = tmp_path / "switch"
    installer.prepare(release, backup, config, installer.BASELINE, unpacked)
    for member in backup.iterdir():
        member.chmod(0o600)
    shutil.copytree(unpacked, store / f"first-officer-{release}")
    original = (config.read_bytes(), pin.read_bytes())
    installer.switch(backup, config, restore=False)
    candidate = (config.read_bytes(), pin.read_bytes())
    assert candidate != original
    assert started[-1] == candidate[0]

    seal = tmp_path / "audit-anchor"
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(seal))
    monkeypatch.delenv("FO_ACCEPTANCE_ROOT", raising=False)
    started.clear()
    stops.clear()
    recorded.clear()

    def runner(entry, _substitutions):
        if "rollback" in entry["argv"]:
            monkeypatch.setattr(sys, "argv", ["install.py", "rollback", str(backup)])
            if installer.main() != 0:
                raise ValueError(f"Release step failed: {entry['what']}")
            return ""
        if entry["what"] == "Confirm the gateway is active after the rollback":
            return ""
        raise AssertionError(entry["what"])

    assert release_tool.main(["rollback", str(backup)], runner) == 0
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert stops == ["stop"]
    assert started == [original[0]]
    assert candidate[0] not in started
    verifier_calls = [args for args in recorded if any(part.endswith("verify-install.py") for part in args)]
    assert verifier_calls == [installer.installed_verifier(backup / "original-openclaw.json", installer.BASELINE)]
    assert "restored and the gateway is active" in capsys.readouterr().out

    stop_count = len(stops)
    with pytest.raises(ValueError, match="missing or does not match"):
        installer.switch(backup, config, restore=False)
    assert len(stops) == stop_count
    assert (config.read_bytes(), pin.read_bytes()) == original

    monkeypatch.setenv("FO_ACCEPTANCE_ROOT", str(tmp_path / "cycle"))
    with pytest.raises(ValueError, match="missing"):
        installer.switch(backup, config, restore=False)
    seal.write_bytes(b"{}\n")
    seal.chmod(0o666)
    with pytest.raises(ValueError, match="not owned by root|not mode-restricted"):
        installer.switch(backup, config, restore=False)
    assert len(stops) == stop_count
    assert (config.read_bytes(), pin.read_bytes()) == original

    monkeypatch.delenv("FO_ACCEPTANCE_ROOT", raising=False)
    config.write_bytes(original[0])
    pin.write_bytes(candidate[1])
    readiness["fail"] = 1
    started.clear()
    with pytest.raises(ValueError, match="prior configuration and service were restored"):
        installer.switch(backup, config, restore=True)
    failed_mixed_rollback_reactivated_candidate = (config.read_bytes(), pin.read_bytes()) == candidate
    assert failed_mixed_rollback_reactivated_candidate is False
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert candidate[0] not in started
    assert started == [original[0], original[0]]

    config.write_bytes(candidate[0])
    pin.write_bytes(candidate[1])
    readiness["fail"] = 1
    started.clear()
    with pytest.raises(ValueError, match="prior configuration and service were restored"):
        installer.switch(backup, config, restore=True)
    failed_complete_rollback_reactivated_candidate = (config.read_bytes(), pin.read_bytes()) == candidate
    assert failed_complete_rollback_reactivated_candidate is False
    assert (config.read_bytes(), pin.read_bytes()) == original
    assert candidate[0] not in started
    assert all(payload == original[0] for payload in started)


def test_standalone_service_pins_the_same_guard_on_both_hosts(tmp_path, monkeypatch):
    import hashlib
    import plistlib
    from types import SimpleNamespace

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("standalone_install", repo / "first-officer/standalone.py")
    standalone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standalone)
    files = {
        standalone.RUNTIME: (repo / "first-officer/segment1/captain-state.py").read_bytes(),
        "first-officer-exec-guard.mjs": b"fixture guard",
        "round6/runtime/first-officer-conductor.mjs": b"fixture conductor",
    }
    stage = standalone.load_module("standby_stage_fixture", repo / "first-officer/stage.py")
    files.update({f"round6/runtime/segment1/{name}": (repo / "first-officer" / source).read_bytes()
                  for name, source in stage.QUEUE_MODULES.items()})
    inventory = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()
    release = tmp_path / ("first-officer-" + hashlib.sha256(inventory).hexdigest())
    for name, data in files.items():
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (release / "SHA256SUMS").write_bytes(inventory)
    config = tmp_path / "test config.json"
    state = tmp_path / "test-state"
    secrets = tmp_path / "gateway-env.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state.mkdir(mode=0o700)
    for name in ("python3", "node", "openclaw"):
        bin_path = runtime / name
        bin_path.write_text("fixture")
        bin_path.chmod(0o700)
    config.write_text(json.dumps({"plugins": {"load": {"paths": [str(release / name) for name in files if name.endswith(".mjs")]}}}))
    config.chmod(0o600)
    secrets.write_text(json.dumps({"OPENCLAW_LITELLM_API_KEY": "fixture-setting"}))
    secrets.chmod(0o600)

    def validate(argv, **options):
        assert argv == [str(runtime / "openclaw"), "config", "validate"]
        assert options["env"]["OPENCLAW_CONFIG_PATH"] == str(config)
        assert options["env"]["OPENCLAW_LITELLM_API_KEY"] == "fixture-setting"
        return SimpleNamespace(returncode=0)

    arguments = (release, runtime / "python3", runtime / "node", runtime / "openclaw", config, state, secrets, validate)
    mac = standalone.prepare("mac-mini", *arguments)
    nuc = standalone.prepare("nucbox", *arguments)
    plist = plistlib.loads(mac)
    assert plist["EnvironmentVariables"]["FO_LOCAL_NODE"] == "mac-mini"
    assert plist["ProgramArguments"][3:] == [str(release / standalone.RUNTIME), "local-run",
                                            str(runtime / "openclaw"), str(secrets)]
    assert 'Environment="FO_LOCAL_NODE=nucbox"' in nuc.decode()
    assert f'"{release / standalone.RUNTIME}" "local-run"' in nuc.decode()
    assert "UserName" not in plist
    assert "RunAtLoad" not in plist
    monkeypatch.setattr(standalone.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_name="gateway", pw_dir=str(tmp_path)))
    daemon_bytes = standalone.prepare("mac-mini", *arguments, mac_launch_daemon=True)
    daemon = plistlib.loads(daemon_bytes)
    assert daemon["UserName"] == "gateway"
    assert daemon["WorkingDirectory"] == str(tmp_path)
    assert daemon["EnvironmentVariables"]["HOME"] == str(tmp_path)
    assert daemon["RunAtLoad"] is True
    assert daemon["ProgramArguments"] == plist["ProgramArguments"]
    assert daemon["KeepAlive"] == {"SuccessfulExit": False}
    with pytest.raises(ValueError, match="unprivileged Mac gateway account"):
        standalone.prepare("nucbox", *arguments, mac_launch_daemon=True)
    with monkeypatch.context() as privileged:
        privileged.setattr(standalone.os, "getuid", lambda: 0)
        with pytest.raises(ValueError, match="unprivileged Mac gateway account"):
            standalone.prepare("mac-mini", *arguments, mac_launch_daemon=True)
    assert b"fixture-setting" not in mac + nuc + daemon_bytes
    broker_dir = tmp_path / "broker-config"
    broker_dir.mkdir(mode=0o700)
    for name in ("ca.pem", "broker.pem", "broker.key", "nuc.pem", "mac.pem", "telegram.json"):
        fixture = broker_dir / name
        fixture.write_text("fixture-broker-setting")
        fixture.chmod(0o600)
    listen = standalone.ipaddress.IPv4Address("127.0.0.1")
    rendered = standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir, listen)
    broker = plistlib.loads(rendered)
    assert broker["Label"] == "local.openclaw.captain-broker"
    assert broker["UserName"] == "gateway"
    assert broker["WorkingDirectory"] == str(state)
    assert broker["RunAtLoad"] is True and broker["Umask"] == 0o077
    assert broker["ProgramArguments"][:4] == [str(runtime / "python3"), "-I", "-B",
                                             str(release / standalone.BROKER)]
    assert broker["ProgramArguments"][broker["ProgramArguments"].index("--telegram-config") + 1] == str(
        broker_dir / "telegram.json")
    assert b"fixture-broker-setting" not in rendered
    queue_config = broker_dir / "project-queue.json"
    queue_env = broker_dir / "queue-environment.json"
    for fixture in (queue_config, queue_env):
        fixture.write_text("fixture-queue-setting")
        fixture.chmod(0o600)
    with pytest.raises(ValueError, match="policies and their private environment together"):
        standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir, listen,
                                      project_queue_config=queue_config)
    rendered = standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir, listen,
                                             project_queue_config=queue_config, queue_environment=queue_env)
    assert plistlib.loads(rendered)["ProgramArguments"][-4:] == [
        "--project-queue-config", str(queue_config), "--queue-environment", str(queue_env),
    ]
    assert b"fixture-queue-setting" not in rendered
    with_memory = standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir, listen,
                                                memory_node=runtime / "node")
    assert plistlib.loads(with_memory)["ProgramArguments"][-2:] == ["--memory-node", str(runtime / "node")]
    with pytest.raises(ValueError, match="private broker interface"):
        standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir,
                                      standalone.ipaddress.IPv4Address("0.0.0.0"))
    (broker_dir / "broker.key").chmod(0o644)
    with pytest.raises(ValueError, match="private local configuration"):
        standalone.prepare_mac_broker(release, runtime / "python3", state, broker_dir, listen)
    settings = json.loads(config.read_bytes())
    settings["channels"] = {"telegram": {"enabled": True, "dmPolicy": "allowlist", "allowFrom": [123456],
                                           "execApprovals": {"approvers": [123456]}}}
    config.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="conversation-hook access"):
        standalone.prepare("nucbox", *arguments)
    settings["plugins"]["entries"] = {"first-officer-conductor": {"hooks": {"allowConversationAccess": True}}}
    settings["channels"]["telegram"]["execApprovals"]["approvers"] = [234567]
    config.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="same single Telegram owner"):
        standalone.prepare("nucbox", *arguments)
    settings["channels"]["telegram"]["execApprovals"]["approvers"] = [123456]
    config.write_text(json.dumps(settings))
    assert standalone.prepare("nucbox", *arguments) == nuc
    secrets.write_text(json.dumps({"FO_LOCAL_NODE": "bobserver"}))
    with pytest.raises(ValueError, match="environment file is invalid"):
        standalone.prepare("nucbox", *arguments)

    secrets.write_text(json.dumps({"OPENCLAW_LITELLM_API_KEY": "fixture-setting"}))
    settings["gateway"] = {"mode": "local", "bind": "loopback", "port": 43000}
    settings["channels"]["telegram"]["groupPolicy"] = "disabled"
    settings["plugins"]["load"]["paths"] = [str(release / "first-officer-exec-guard.mjs"),
                                             str(release / "round6/runtime/first-officer-conductor.mjs")]
    settings["plugins"]["allow"] = ["first-officer-conductor", "first-officer-exec-guard", "telegram"]
    settings["plugins"]["entries"].update({"first-officer-exec-guard": {}, "telegram": {}})
    settings["models"] = {"providers": {"litellm": {"baseUrl": "http://127.0.0.1:44680/v1",
                                                   "models": [{"id": "qwen-local-fast"}]}}}
    settings["agents"] = {"defaults": {"model": {"primary": "litellm/qwen-local-fast"}}}
    settings["messages"] = {"inbound": {"debounceMs": 300, "byChannel": {"telegram": 300, "slack": 500}}}
    config.write_text(json.dumps(settings))
    profile = tmp_path / "broker.json"
    key = tmp_path / "fixture.key"
    key.touch(mode=0o600)
    profile.write_text(json.dumps({"hostname": "broker.invalid", "port": 4372, "bot_id": 234567,
                                   "key": str(key), "cert": str(key), "ca": str(key)}))
    profile.chmod(0o600)

    def validate_standby(argv, **options):
        assert argv == [str(runtime / "openclaw"), "config", "validate"]
        checked = json.loads(Path(options["env"]["OPENCLAW_CONFIG_PATH"]).read_bytes())
        telegram = checked["channels"]["telegram"]
        assert telegram["apiRoot"] == "http://127.0.0.1:1"
        assert telegram["replyToMode"] == "all"
        assert checked["messages"]["inbound"] == {"debounceMs": 300, "byChannel": {"telegram": 0, "slack": 500}}
        assert json.loads(config.read_bytes())["messages"]["inbound"]["byChannel"]["telegram"] == 300
        assert telegram["botToken"].startswith("234567:")
        assert options["env"]["OPENCLAW_LITELLM_API_KEY"] == "fixture-setting"
        return SimpleNamespace(returncode=0)

    standby_args = (*arguments[:-1], validate_standby)
    boot = standalone.prepare("mac-mini", *standby_args, mac_launch_daemon=True, standby_profile=profile)
    systemd = standalone.prepare("nucbox", *standby_args, standby_profile=profile)
    plist = plistlib.loads(boot)
    assert plist["UserName"] == "gateway" and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][3:] == [str(release / standalone.STANDBY), str(runtime / "openclaw"),
                                            str(config), str(secrets), str(profile), "--prewarm-on-start"]
    assert str(release / standalone.STANDBY) in systemd.decode()
    assert b"--prewarm-on-start" not in systemd
    assert b"fixture-setting" not in boot + systemd
    settings["channels"]["telegram"]["botToken"] = "fixture-token"
    config.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="token-free"):
        standalone.prepare("nucbox", *standby_args, standby_profile=profile)
