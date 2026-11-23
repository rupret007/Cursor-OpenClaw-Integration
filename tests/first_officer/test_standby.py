import runpy
from pathlib import Path

import pytest


standby = runpy.run_path(str(Path(__file__).resolve().parents[2] / "first-officer" / "standby.py"))
Authority = standby["StandbyAuthority"]
LeaseRejected = standby["LeaseRejected"]
PRIMARY = standby["PRIMARY"]
STANDBY = standby["STANDBY"]


def test_standby_waits_ninety_seconds_since_last_renewal(tmp_path):
    now = 0
    with Authority(tmp_path, clock=lambda: now) as authority:
        assert authority.claim(STANDBY) is None
        nuc = authority.claim(PRIMARY)
        assert nuc.epoch == 1
        now = 15
        authority.renew(nuc)
        now = 104.99
        assert authority.claim(STANDBY) is None
        now = 105
        assert authority.claim(PRIMARY) is None
        with pytest.raises(LeaseRejected):
            authority.renew(nuc)
        mac = authority.claim(STANDBY)
        assert mac.epoch == 2
        assert authority.claim(PRIMARY) is None
        authority.renew(mac)


@pytest.mark.parametrize("kind", ["telegram-reply", "hatchet-submit", "restate-submit"])
def test_partition_and_return_cannot_repeat_effects(tmp_path, kind):
    now = 0
    effects = []
    with Authority(tmp_path, clock=lambda: now) as authority:
        nuc = authority.claim(PRIMARY)

        def accepted():
            effects.append("accepted")

        def interrupted():
            effects.append("maybe accepted")
            raise TimeoutError

        assert authority.perform(nuc, kind, "stable-confirmed", accepted) == "confirmed"
        with pytest.raises(TimeoutError):
            authority.perform(nuc, kind, "stable-unconfirmed", interrupted)
        now = 90
        with pytest.raises(LeaseRejected):
            authority.perform(nuc, kind, "new-effect", accepted)
        mac = authority.claim(STANDBY)
        with pytest.raises(LeaseRejected):
            authority.perform(nuc, kind, "new-effect", accepted)
        assert authority.perform(mac, kind, "stable-confirmed", accepted) == "confirmed"
        assert authority.perform(mac, kind, "stable-unconfirmed", accepted) == "unconfirmed"
        assert authority.perform(mac, kind, "new-effect", accepted) == "confirmed"

    assert effects == ["accepted", "maybe accepted", "accepted"]


def test_expiry_during_reservation_blocks_send_and_keeps_operation_available(tmp_path):
    ticks = iter((0, 0, 0, 89, 90, 90, 90, 90, 90))
    with Authority(tmp_path, clock=lambda: next(ticks)) as authority:
        nuc = authority.claim(PRIMARY)
        with pytest.raises(LeaseRejected):
            authority.perform(nuc, "telegram-reply", "reply", pytest.fail)
        mac = authority.claim(STANDBY)
        effects = []
        assert authority.perform(mac, "telegram-reply", "reply", lambda: effects.append("sent")) == "confirmed"
        assert effects == ["sent"]


def test_broker_restart_preserves_epoch_and_receipts_and_prevents_parallel_broker(tmp_path):
    now = 0
    with Authority(tmp_path, clock=lambda: now) as first:
        nuc = first.claim(PRIMARY)
        assert first.perform(nuc, "telegram-reply", "original", lambda: None) == "confirmed"
        with pytest.raises(BlockingIOError):
            Authority(tmp_path, clock=lambda: now)
    with Authority(tmp_path, clock=lambda: now) as second:
        with pytest.raises(LeaseRejected):
            second.perform(nuc, "telegram-reply", "new", pytest.fail)
        now = 89
        assert second.claim(STANDBY) is None
        now = 90
        mac = second.claim(STANDBY)
        assert mac.epoch == nuc.epoch + 1
        assert second.perform(mac, "telegram-reply", "original", pytest.fail) == "confirmed"
