import json
import runpy
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest


STANDBY_SCRIPT = Path(__file__).resolve().parents[2] / "first-officer" / "standby.py"
standby = runpy.run_path(str(STANDBY_SCRIPT))
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


def test_planned_handback_survives_restart_and_preserves_uncertain_effect(tmp_path):
    now = 0
    with Authority(tmp_path, clock=lambda: now) as broker:
        nuc = broker.claim(PRIMARY)
        now = 90
        mac = broker.claim(STANDBY)

        def interrupted():
            raise TimeoutError

        with pytest.raises(TimeoutError):
            broker.perform(mac, "telegram-reply", "pending", interrupted)
        with pytest.raises(LeaseRejected):
            broker.plan_handoff(mac, PRIMARY)

    with Authority(tmp_path, clock=lambda: now) as offline:
        assert offline.plan_handoff(mac, PRIMARY) == "planned"
        assert offline.plan_handoff(mac, PRIMARY) == "planned"
        with pytest.raises(LeaseRejected):
            offline.plan_handoff(nuc, STANDBY)

    with Authority(tmp_path, clock=lambda: now) as broker:
        assert broker.claim(PRIMARY) is None
        now = 180
        assert broker.claim(STANDBY) is None
        returned = broker.claim(PRIMARY)
        assert returned.epoch == mac.epoch + 1
        assert broker.perform(returned, "telegram-reply", "pending", pytest.fail) == "unconfirmed"
        with pytest.raises(LeaseRejected):
            broker.renew(mac)


def test_planned_handoff_rollback_requires_exact_previous_target(tmp_path):
    now = 0
    with Authority(tmp_path, clock=lambda: now) as broker:
        nuc = broker.claim(PRIMARY)
    with Authority(tmp_path, clock=lambda: now) as offline:
        offline.plan_handoff(nuc, STANDBY)
        with pytest.raises(LeaseRejected):
            offline.plan_handoff(nuc, PRIMARY)
        assert offline.plan_handoff(nuc, PRIMARY, replaces=STANDBY) == "planned"
        assert offline.plan_handoff(nuc, PRIMARY, replaces=STANDBY) == "planned"

    with Authority(tmp_path, clock=lambda: now) as broker:
        now = 90
        assert broker.claim(STANDBY) is None
        assert broker.claim(PRIMARY).epoch == nuc.epoch + 1

    with Authority(tmp_path, clock=lambda: now) as offline:
        with pytest.raises(LeaseRejected):
            offline.plan_handoff(nuc, STANDBY)


def test_handoff_command_refuses_open_broker_and_missing_journal(tmp_path):
    command = [sys.executable, "-I", "-B", str(STANDBY_SCRIPT), "--state", str(tmp_path),
               "--source", PRIMARY, "--epoch", "1", "--destination", STANDBY]

    def run():
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=3)

    assert run().returncode == 1
    assert not (tmp_path / "authority.sqlite").exists()
    with Authority(tmp_path) as broker:
        assert broker.claim(PRIMARY).epoch == 1
        blocked = run()
        assert blocked.returncode == 1
        assert "journal is in use" in blocked.stderr
    assert run().returncode == 0
    assert run().returncode == 0


def test_export_fences_source_and_retries_without_overwriting(tmp_path):
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    destination = tmp_path / "snapshot"
    transfer = "12" * 16
    command = [sys.executable, "-I", "-B", str(STANDBY_SCRIPT), "--state", str(source),
               "--source", PRIMARY, "--epoch", "1", "--destination", STANDBY,
               "--export-path", str(destination), "--transfer-id", transfer]

    def run(*argv):
        return subprocess.run([*command, *argv], check=False, capture_output=True, timeout=3)

    with Authority(source) as first:
        grant = first.claim(PRIMARY)
        assert first.perform(grant, "telegram-reply", "prior", lambda: None) == "confirmed"
        assert run().returncode == 1
        assert not destination.exists()

    inbox = source / "telegram-inbox.sqlite"
    inbox.touch(mode=0o600)
    with sqlite3.connect(inbox) as db:
        db.execute("CREATE TABLE pending (id INT)")
        db.execute("INSERT INTO pending VALUES (42)")
    (source / "project-queue-receipts.jsonl").write_text('{"run": "original"}\n')
    (source / "project-queue-receipts.jsonl").chmod(0o600)
    (source / "project-scope.json").write_text("{}")

    assert run().returncode == 0
    assert run().returncode == 0
    with pytest.raises(LeaseRejected, match="sealed"):
        Authority(source)
    with pytest.raises(LeaseRejected, match="sealed"):
        Authority(destination)
    with sqlite3.connect(destination / inbox.name) as db:
        assert db.execute("SELECT id FROM pending").fetchone() == (42,)
    assert (destination / "project-queue-receipts.jsonl").read_text() == '{"run": "original"}\n'
    assert not (destination / "project-scope.json").exists()
    receipt = json.loads((destination / "transfer.json").read_bytes())
    assert receipt["transfer"] == transfer
    assert set(receipt["journals"]) == {"authority.sqlite", inbox.name, "project-queue-receipts.jsonl"}

    assert run("--transfer-id", "34" * 16).returncode == 1
    (destination / "project-queue-receipts.jsonl").write_text("damaged")
    assert run().returncode == 1
    assert (destination / "project-queue-receipts.jsonl").read_text() == "damaged"


def test_interrupted_export_keeps_source_sealed_for_same_retry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    snapshot = tmp_path / "snapshot"
    transfer = "ab" * 16
    with Authority(source) as broker:
        grant = broker.claim(PRIMARY)

    with Authority(source, export_id=transfer) as journal:
        with monkeypatch.context() as injected:
            def interrupted(path):
                raise OSError("fixture interruption")

            injected.setitem(standby["export_snapshot"].__globals__, "sync_path", interrupted)
            with pytest.raises(OSError, match="fixture interruption"):
                standby["export_snapshot"](journal, source, snapshot, grant, STANDBY, transfer)
        assert not snapshot.exists()

    with pytest.raises(LeaseRejected, match="sealed"):
        Authority(source)
    with Authority(source, export_id=transfer) as recovery:
        standby["export_snapshot"](recovery, source, snapshot, grant, STANDBY, transfer)
    assert (snapshot / "transfer.json").is_file()
