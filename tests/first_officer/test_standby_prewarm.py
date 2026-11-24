import fcntl
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    return importlib.import_module("standby_launch")


def config(release):
    known = ("first-officer-conductor", "first-officer-exec-guard", "telegram")
    return {
        "gateway": {"mode": "local", "bind": "loopback", "port": 43191, "remote": {"endpoint": "ignored"}},
        "plugins": {"allow": list(known), "entries": {name: {"enabled": True} for name in known},
                    "load": {"paths": [str(release / "first-officer-exec-guard.mjs"),
                                       str(release / "round6/runtime/first-officer-conductor.mjs")]}},
        "agents": {"defaults": {"model": {"primary": "litellm/qwen-local-fast"}}},
        "models": {"providers": {"litellm": {"baseUrl": "http://localhost:44680/v1",
                                             "models": [{"id": "qwen-local-fast"}]}}},
        "channels": {"telegram": {"enabled": True, "dmPolicy": "allowlist", "groupPolicy": "disabled",
                                  "allowFrom": [101], "execApprovals": {"approvers": [101]}}},
        "hooks": {"enabled": True},
    }


def test_prewarm_uses_an_idle_relay_and_the_same_private_worker_cache(launcher, tmp_path, monkeypatch):
    release, state = tmp_path / "release", tmp_path / "state"
    state.mkdir(mode=0o700)
    runtime = release / "round6/runtime/segment1/queue-runtime"
    monkeypatch.setattr(launcher, "__file__", str(runtime / "standby_launch.py"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "ignored"))
    template = tmp_path / "template.json"
    environment = tmp_path / "environment.json"
    for file, data in ((template, config(release)), (environment, {})):
        file.write_text(json.dumps(data))
        file.chmod(0o600)
    lifecycle = []

    class Gateway:
        def __init__(self, relay, epoch, owner, bot, executable, settings, env, scratch):
            assert relay.next_telegram_message(epoch) is None
            assert "hooks" not in settings and "remote" not in settings["gateway"]
            assert Path(env["OPENCLAW_STATE_DIR"]) == Path(env["HOME"]) == scratch
            assert scratch.parent == state
            assert Path(env["TMPDIR"]) == state / "standby-worker-cache"
            lifecycle.append("start")

        def exited(self):
            return False

        def ready(self):
            return True

        def stop(self):
            lifecycle.append("stop")

    monkeypatch.setattr(launcher, "BrokeredGateway", Gateway)
    with launcher.open_private(state, "standby-supervisor.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            launcher.prewarm(tmp_path / "openclaw", template, environment)
    assert lifecycle == []
    launcher.prewarm(tmp_path / "openclaw", template, environment)
    assert lifecycle == ["start", "stop"]
    assert not list(state.glob(".standby-prewarm-*"))
    assert (state / "standby-worker-cache").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("unsafe", ["remote", "fallback", "extra-plugin"])
def test_prewarm_rejects_routes_that_could_escape_its_scope(launcher, tmp_path, unsafe):
    template = config(tmp_path)
    if unsafe == "remote":
        template["models"]["providers"]["litellm"]["baseUrl"] = "https://example.invalid/v1"
    elif unsafe == "fallback":
        template["agents"]["defaults"]["model"]["fallbacks"] = ["litellm/extra"]
    else:
        template["plugins"]["allow"].append("unreviewed-plugin")
    with pytest.raises(ValueError, match="pinned Captain plugins|single local Qwen route"):
        launcher.prewarm_template(template, tmp_path)


@pytest.mark.parametrize("interrupted", [False, True])
def test_boot_prewarm_finishes_before_supervision(launcher, tmp_path, monkeypatch, interrupted):
    events = []
    profile = {"hostname": "broker.invalid", "port": 4372, "bot_id": 234567,
               "cert": "cert", "key": "key", "ca": "ca"}
    monkeypatch.setattr(launcher, "profile_from", lambda _: profile)
    monkeypatch.setattr(launcher, "template_from", lambda _: ({}, 101))
    monkeypatch.setattr(launcher, "gateway_env", lambda _: {})
    monkeypatch.setattr(launcher, "gateway_state", lambda _: (tmp_path, {}))
    monkeypatch.setattr(launcher, "Client", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.signal, "signal", lambda *args: None)

    def warm(executable, template, state, environment, stop):
        with launcher.open_private(state, "standby-supervisor.lock") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        events.append("warm")
        if interrupted:
            stop.set()
            return None
        return 0.5

    class Supervisor:
        def __init__(self, *args, **kwargs):
            events.append("supervisor")

        def run(self, stop, update):
            events.append("run")

    monkeypatch.setattr(launcher, "warm_gateway", warm)
    monkeypatch.setattr(launcher, "Supervisor", Supervisor)
    launcher.run("openclaw", "template", "environment", "profile", prewarm_on_start=True)
    assert events == (["warm"] if interrupted else ["warm", "supervisor", "run"])
