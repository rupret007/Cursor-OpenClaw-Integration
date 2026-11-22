from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
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


def test_standalone_handoff_signed_and_restart_safe(captain, tmp_path):
    module, _, _ = captain
    mac_root = tmp_path / "mac"
    nuc_root = tmp_path / "nuc"

    def prepared(root, node):
        root.mkdir(mode=0o700)
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-f", str(root / "handoff-key")], check=True, capture_output=True)
        authority = module.StandaloneAuthority(node, root)
        return authority, module.Controller(authority, private=root / "reviews")

    mac, mac_reviews = prepared(mac_root, "mac-mini")
    nuc, nuc_reviews = prepared(nuc_root, "nucbox")
    for target, source in ((mac_root, nuc_root), (nuc_root, mac_root)):
        (target / "peer.pub").write_bytes((source / "handoff-key.pub").read_bytes())
        (target / "peer.pub").chmod(0o600)

    with pytest.raises(ValueError, match="uninitialized Mac"):
        nuc.initialize()
    assert not nuc_reviews.view()["armed"]
    assert mac.initialize() == {"epoch": 1}

    owner = "123456"
    grant = {"message": 100, "epoch": 1, "owner": owner}
    assert mac_reviews.control("grant", grant) == {"result": "grant"}
    assert mac_reviews.control("handback", {**grant, "message": 102}) == {"result": "operator-transfer"}

    review = {"owner": owner, "message": 101, "lane": "fo-ci-" + "a" * 24,
              "state": "running", "pr": 33, "head": "a" * 40, "epoch": 1, "ci": "passing"}
    with mac_reviews.locked(write=True) as data:
        data["jobs"].append(review)
    with pytest.raises(ValueError, match="active review"):
        mac.release(1, mac_reviews.locked)
    assert mac_reviews.view()["armed"]

    mac_reviews.finish({"lane": review["lane"], "state": "verified"})
    receipt = mac.release(1, mac_reviews.locked)
    assert mac_reviews.control("grant", {**grant, "message": 103}) == {"result": "stale"}
    assert not mac_reviews.view()["armed"]
    assert mac.release(1, mac_reviews.locked) == receipt
    modified = {**receipt, "transfer": {**receipt["transfer"], "epoch": 3}}
    with pytest.raises(subprocess.CalledProcessError):
        nuc.accept(modified, nuc_reviews.file, nuc_reviews.locked)
    assert not nuc_reviews.view()["armed"]
    assert nuc.accept(receipt, nuc_reviews.file, nuc_reviews.locked) == {"epoch": 2}

    restarted = module.StandaloneAuthority("nucbox", nuc_root)
    nuc_reviews = module.Controller(restarted, private=nuc_root / "reviews")
    view = nuc_reviews.view()
    assert view["armed"] and view["lease"]["epoch"] == 2
    assert view["jobs"][0]["state"] == "verified"
    assert view["jobs"][0]["delivery"] == "delivery-unverified"
    assert view["grant"] is None
    assert nuc_reviews.control("grant", grant) == {"result": "stale"}
    with pytest.raises(ValueError, match="stale"):
        restarted.accept(receipt, nuc_reviews.file, nuc_reviews.locked)
    assert not mac_reviews.view()["armed"]

    rollback = restarted.release(2, nuc_reviews.locked)
    assert mac.accept(rollback, mac_reviews.file, mac_reviews.locked) == {"epoch": 3}
    assert mac_reviews.view()["armed"] and not nuc_reviews.view()["armed"]
    with pytest.raises(ValueError, match="stale"):
        restarted.accept(receipt, nuc_reviews.file, nuc_reviews.locked)
    with pytest.raises(ValueError, match="uninitialized Mac"):
        mac.initialize()


def test_standalone_transfer_waits_for_gateway_child_after_supervisor_exit(captain, tmp_path):
    module, _, _ = captain
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    for key in ("handoff-key", "peer"):
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-f", str(root / key)], check=True, capture_output=True)
    (root / "peer.pub").chmod(0o600)
    authority = module.StandaloneAuthority("mac-mini", root)
    controller = module.Controller(authority, private=root / "reviews")
    gateway = tmp_path / "openclaw"
    gateway.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(tmp_path / 'pid')!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    gateway.chmod(0o700)
    with pytest.raises(ValueError, match="parked"):
        authority.run_gateway(gateway)
    authority.initialize()
    launcher = (
        "import importlib.util, sys; from pathlib import Path; "
        "s = importlib.util.spec_from_file_location('captain_state', sys.argv[1]); "
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "sys.exit(m.StandaloneAuthority('mac-mini', Path(sys.argv[2])).run_gateway(Path(sys.argv[3])))"
    )
    supervisor = subprocess.Popen([sys.executable, "-B", "-c", launcher, module.__file__, str(root), str(gateway)])
    pid = None
    try:
        pid_path = tmp_path / "pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pid = int(pid_path.read_text())
                break
            except (FileNotFoundError, ValueError):
                time.sleep(0.02)
        assert pid is not None
        with pytest.raises(BlockingIOError):
            authority.release(1, controller.locked)
        supervisor.kill()
        supervisor.wait(timeout=5)
        with pytest.raises(BlockingIOError):
            authority.release(1, controller.locked)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if pid is not None:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            assert authority.release(1, controller.locked)["transfer"]["epoch"] == 2
            break
        except BlockingIOError:
            time.sleep(0.02)
    else:
        pytest.fail("Gateway child retained the handoff lock after exiting")
    with pytest.raises(ValueError, match="parked"):
        authority.run_gateway(gateway)


def test_standalone_capacity_counts_configured_live_process(captain, tmp_path):
    module, _, _ = captain
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    authority = module.StandaloneAuthority("nucbox", root)
    binary = tmp_path / "fo-cap-fixture"
    binary.symlink_to("/bin/sleep")
    policy = root / "capacity.json"
    module.save(policy, {"commands": [binary.name], "limit": 1})
    assert module.local_capacity(authority)["used"] == 0
    with subprocess.Popen([str(binary), "30"]) as process:
        try:
            reading = module.local_capacity(authority)
            assert (reading["node"], reading["used"], reading["limit"]) == ("nucbox", 1, 1)
        finally:
            process.terminate()
    assert module.local_capacity(authority)["used"] == 0
    module.save(policy, {"commands": [], "limit": 1})
    with pytest.raises(ValueError, match="Invalid standalone capacity"):
        module.local_capacity(authority)
