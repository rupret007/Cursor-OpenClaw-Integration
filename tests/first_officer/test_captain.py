from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def captain(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "first-officer/segment1/captain-state.py"
    spec = importlib.util.spec_from_file_location("captain_state", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(module, "LANES", tmp_path / "lanes")
    module.LANES.mkdir()
    lease = SimpleNamespace(holder="first-officer", state="held", epoch=11)

    class Fence:
        @contextmanager
        def _locked(self):
            yield

        def status(self):
            return lease

        def _read_unlocked(self):
            return lease

        def _write_unlocked(self, record):
            assert record is lease

    fence = Fence()
    watchdog = SimpleNamespace(
        private_authority=lambda path: True, AUTHORITY=tmp_path,
        load_fence=lambda: lambda path: fence, enabled=lambda: True,
    )
    return module, lease, watchdog


def test_scope_deduplicates_launch_and_rejects_pause_stale_epoch_and_changed_owner(captain, monkeypatch):
    module, lease, watchdog = captain
    controller = module.Controller(watchdog)
    owner = "123456"
    grant = {"message": 100, "epoch": 11, "profile": 1, "owner": owner}
    plan = {**grant, "message": 101, "pr": 33, "head": "a" * 40, "ci": "passing"}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[4:7] == ["--fence", "11", "launch"]
        prompt = module.LANES / command[-2]
        assert module.digest(prompt.read_bytes()) == command[-1]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert controller.submit(plan) == {"result": "stale"}
    assert controller.control("grant", grant) == {"result": "grant"}
    assert controller.submit({**plan, "owner": "112233"}) == {"result": "stale"}
    assert controller.submit(plan)["result"] == "running"
    assert controller.submit(plan) == {"result": "duplicate"}
    assert len(calls) == 1
    assert controller.control("pause", {**grant, "message": 102}) == {"result": "pause"}
    assert controller.submit({**plan, "message": 103, "head": "b" * 40}) == {"result": "stale"}
    assert controller.control("grant", grant) == {"result": "stale"}
    assert controller.control("handback", {**grant, "message": 104}) == {"result": "handback"}
    assert lease.state == "handback-pending"
    lease.epoch = 12
    lease.state = "held"
    reopened = module.Controller(watchdog)
    assert reopened.submit({**plan, "epoch": 12, "message": 106}) == {"result": "stale"}
    assert reopened.view()["active"]["pr"] == 33


def test_replayed_telegram_update_cannot_launch_again_after_the_pr_head_changes(captain, monkeypatch):
    module, _, watchdog = captain
    launched = []

    def launch(command, **_kwargs):
        launched.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", launch)
    controller = module.Controller(watchdog)
    grant = {"message": 100, "epoch": 11, "owner": "123456"}
    controller.control("grant", grant)
    request = {**grant, "message": 101, "pr": 33, "head": "a" * 40, "ci": "passing"}
    first = controller.submit(request)
    assert first["result"] == "running"
    controller.finish({"lane": first["job"]["lane"], "state": "verified"})

    restarted = module.Controller(watchdog)
    new_head = {**request, "head": "b" * 40}
    assert restarted.submit(new_head) == {"result": "duplicate"}
    assert len(launched) == 1
    assert restarted.submit({**new_head, "message": 102})["result"] == "running"
    assert len(launched) == 2


def test_failed_launch_is_persisted_through_a_restart_and_cannot_be_replayed(captain, monkeypatch):
    module, _, watchdog = captain
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    controller = module.Controller(watchdog)
    req = {"message": 100, "epoch": 11, "profile": 1, "owner": "123456"}
    assert controller.control("grant", req) == {"result": "grant"}
    plan = {**req, "message": 101, "pr": 12, "head": "a" * 40, "ci": "failing"}
    job = controller.submit(plan)["job"]
    reopened = module.Controller(watchdog)
    assert reopened.view()["active"]["launchRejected"] is True
    assert reopened.poll(job["lane"]) == {"worker": "no-launch-receipt"}
    finished = reopened.finish({"lane": job["lane"], "state": "worker-failed"})
    assert finished["job"]["delivery"] == "delivery-unverified"
    assert reopened.submit(plan) == {"result": "duplicate"}
