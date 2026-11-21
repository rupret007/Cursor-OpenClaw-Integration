from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def installed(tmp_path):
    helper = Path(__file__).resolve().parents[2] / "first-officer/captain-arm.py"
    spec = importlib.util.spec_from_file_location("captain_arm", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flag = tmp_path / "takeover.enabled"
    flag.write_text("OFF\n")
    flag.chmod(0o600)
    record = SimpleNamespace(holder="captain", state="held", epoch=12, renewed_at=1000, to_json=lambda: {})

    class Fence:
        locked = False

        @contextmanager
        def _locked(self):
            assert not self.locked
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

        def _read_unlocked(self):
            assert self.locked
            return record

    fence = Fence()

    def observe(*args):
        assert fence.locked
        return lock_state.outcome

    lock_state = SimpleNamespace(outcome="takeover-handoff")
    watchdog = SimpleNamespace(
        trusted=lambda path: True, AUTHORITY=tmp_path, load_fence=lambda: lambda path: fence,
        FLAG=flag, flag_value=lambda: flag.read_text().strip(), timestamp=lambda value: isinstance(value, int),
        HEARTBEAT=None, STATUS=None, observe=observe,
    )
    return module.change_arming, watchdog, record, lock_state


def test_arm_requires_exact_epoch_fresh_check_in_and_explicit_handoff(installed):
    arm, watchdog, _, observed = installed
    with pytest.raises(ValueError, match="expected epoch"):
        arm(watchdog, "arm", 11, clock=lambda: 1001)
    with pytest.raises(ValueError, match="fresh"):
        arm(watchdog, "arm", 12, clock=lambda: 2000)
    observed.outcome = "takeover-stale"
    with pytest.raises(ValueError, match="explicit"):
        arm(watchdog, "arm", 12, clock=lambda: 1001)
    assert watchdog.flag_value() == "OFF"
    observed.outcome = "takeover-handoff"
    assert arm(watchdog, "arm", 12, clock=lambda: 1001) == "ON"
    assert watchdog.flag_value() == "ON"
    assert watchdog.FLAG.stat().st_mode & 0o077 == 0


def test_disarm_waits_for_handback(installed):
    arm, watchdog, record, _ = installed
    arm(watchdog, "arm", 12, clock=lambda: 1001)
    record.holder = "first-officer"
    record.epoch = 13
    with pytest.raises(ValueError, match="does not hold"):
        arm(watchdog, "disarm", 13)
    assert watchdog.flag_value() == "ON"
    record.holder = "captain"
    record.epoch = 14
    assert arm(watchdog, "disarm", 14) == "OFF"
    assert watchdog.flag_value() == "OFF"
