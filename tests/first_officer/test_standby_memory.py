import importlib
from pathlib import Path

import pytest


def test_only_confirmed_owner_memory_survives_transfer(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    memory_module = importlib.import_module("standby_memory")
    lease = importlib.import_module("standby")
    telegram = importlib.import_module("standby_telegram")
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    owner, bot = 101, 202
    inbox = telegram.TelegramInbox(source, owner, bot)
    memory = memory_module.BrokerMemory(source, owner, bot, lambda fact: fact != "rejected-fixture", clock=lambda: 0)

    def update(message, text, author=owner):
        return {"update_id": message, "message": {"message_id": message, "date": 1, "text": text,
                "chat": {"id": author, "type": "private"}, "from": {"id": author, "is_bot": False}}}

    def command(authority, grant, message, text, operation, topic=None, *, author=owner):
        inbox.ingest([update(message, text, author)])
        response = memory.run(authority, grant, inbox, message, operation, topic)
        inbox.complete(message, "confirmed")
        return response

    with lease.StandbyAuthority(source, clock=lambda: 0) as broker:
        nuc = broker.claim(lease.PRIMARY)
        with pytest.raises(ValueError, match="current owner message"):
            command(broker, nuc, 4, "remember marker: fixture", "remember", author=303)
        assert command(broker, nuc, 5, "remember marker: rejected-fixture", "remember") == {"state": "unsafe"}
        proposal = command(broker, nuc, 6, "remember marker: violet tessellation", "remember")
        assert proposal["state"] == "pending"
        assert command(broker, nuc, 7, "recall marker", "recall", "marker") == {"state": "missing"}
        confirm = "confirm memory " + proposal["code"]
        inbox.ingest([update(8, confirm)])
        for _ in range(2):
            assert memory.run(broker, nuc, inbox, 8, "confirm") == {"state": "confirmed", "topic": "marker"}
        inbox.complete(8, "confirmed")
        pending = command(broker, nuc, 9, "remember marker: later fixture", "remember")

    transfer_id = "ab" * 16
    snapshot = tmp_path / "snapshot"
    with lease.StandbyAuthority(source, export_id=transfer_id) as stopped:
        lease.export_snapshot(stopped, source, snapshot, nuc, lease.STANDBY, transfer_id)
    assert lease.import_snapshot(destination, snapshot, nuc, lease.STANDBY, transfer_id)
    inbox = telegram.TelegramInbox(destination, owner, bot)
    with pytest.raises(ValueError, match="different Telegram owner or bot"):
        memory_module.BrokerMemory(destination, owner, 303, lambda fact: True)
    memory = memory_module.BrokerMemory(destination, owner, bot, lambda fact: True, clock=lambda: 90)
    now = 0
    with lease.StandbyAuthority(destination, clock=lambda: now) as broker:
        now = 90
        mac = broker.claim(lease.STANDBY)
        assert mac.epoch == nuc.epoch + 1
        with pytest.raises(lease.LeaseRejected):
            memory.run(broker, nuc, inbox, 10, "recall", "marker")
        assert command(broker, mac, 10, "confirm memory " + pending["code"], "confirm") == {"state": "expired"}
        assert command(broker, mac, 11, "recall marker", "recall", "marker") == {
            "state": "saved", "topic": "marker", "statement": "violet tessellation",
        }
