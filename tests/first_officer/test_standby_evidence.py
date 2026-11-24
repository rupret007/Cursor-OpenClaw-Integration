import importlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


def test_delivery_report_migrates_old_receipts_and_masks_private_data(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "first-officer"
    monkeypatch.syspath_prepend(str(source))
    lease = importlib.import_module("standby")
    telegram = importlib.import_module("standby_telegram")
    for name in ("authority.sqlite", "telegram-inbox.sqlite"):
        (tmp_path / name).touch(mode=0o600)
    with sqlite3.connect(tmp_path / "authority.sqlite") as db:
        db.execute("CREATE TABLE effects (kind TEXT, operation TEXT, state TEXT, PRIMARY KEY (kind, operation))")
        db.execute("INSERT INTO effects VALUES ('telegram-reply', 'message:11:final', 'confirmed')")
    with sqlite3.connect(tmp_path / "telegram-inbox.sqlite") as db:
        db.execute("CREATE TABLE inbox (message_id INTEGER PRIMARY KEY, sent_at INTEGER, text TEXT, outcome TEXT)")
        db.execute("INSERT INTO inbox VALUES (11, 1, NULL, 'confirmed')")

    now = 1000
    monkeypatch.setattr(lease, "delivery_milliseconds", lambda: now)
    monkeypatch.setattr(telegram, "delivery_milliseconds", lambda: now)
    inbox = telegram.TelegramInbox(tmp_path, 101, 202)

    def ingest(message, sent_at):
        inbox.ingest([{"update_id": message, "message": {"message_id": message, "date": sent_at,
                       "text": "Private synthetic fixture body", "chat": {"id": 101, "type": "private"},
                       "from": {"id": 101, "is_bot": False}}}])

    with lease.StandbyAuthority(tmp_path) as authority:
        grant = authority.claim(lease.PRIMARY)
        ingest(12, 2)
        now = 1010
        authority.send_telegram_progress(12, "typing", lambda: None)
        now = 1080
        authority.send_telegram_progress(12, "ack", lambda: None, grant=grant)
        now = 1400
        authority.perform(grant, "telegram-reply", "message:12:final", lambda: None)
        now = 1750
        authority.perform(grant, "telegram-reply", "message:12:final", pytest.fail)
        inbox.complete(12, "confirmed")

        ingest(13, 3)
        monkeypatch.setattr(lease, "DELIVERY_CLOCK", "another-process-value")
        authority.perform(grant, "telegram-reply", "message:13:final", lambda: None)
        inbox.complete(13, "confirmed")
        ingest(14, 4)

        def fail():
            raise TimeoutError

        with pytest.raises(TimeoutError):
            authority.perform(grant, "telegram-reply", "message:14:final", fail)
        inbox.complete(14, "unconfirmed")

    result = subprocess.run([sys.executable, "-I", "-B", str(source / "standby.py"), "delivery-report",
                             "--state", str(tmp_path), "--since", "1"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "Private synthetic fixture body" not in result.stdout
    assert "message_id" not in result.stdout
    uncertain, transferred, measured, legacy = json.loads(result.stdout)["receipts"]
    assert uncertain == {"outcome": "unconfirmed", "phases": {
        "final": {"state": "unconfirmed", "relayMs": None, "host": None, "epoch": None},
    }}
    assert transferred["phases"]["final"]["relayMs"] is None
    assert legacy["phases"]["final"]["relayMs"] is None
    assert measured["phases"] == {
        "typing": {"state": "confirmed", "relayMs": 10, "host": None, "epoch": None},
        "ack": {"state": "confirmed", "relayMs": 80, "host": lease.PRIMARY, "epoch": 1},
        "final": {"state": "confirmed", "relayMs": 400, "host": lease.PRIMARY, "epoch": 1},
    }


@pytest.mark.parametrize("case", ["survivor", "old-lease", "ambiguous", "unconfirmed"])
def test_delivery_observer_requires_one_confirmed_receipt_from_the_survivor(tmp_path, monkeypatch, case):
    source = Path(__file__).resolve().parents[2] / "first-officer"
    monkeypatch.syspath_prepend(str(source))
    launcher = importlib.import_module("standby_launch")
    profile = tmp_path / "profile.json"
    verifies = []

    def verify(path, holder, epoch, timeout, *, previous=None):
        assert (path, holder, epoch) == (profile, "mac-mini", 2)
        assert 0 < timeout <= 120
        verifies.append(previous)

    observed = {"outcome": "confirmed", "phases": {
        "final": {"state": "confirmed", "host": "mac-mini", "epoch": 2, "relayMs": 95000},
    }}
    if case == "old-lease":
        observed["phases"]["final"].update(host="nucbox", epoch=1)
    if case == "unconfirmed":
        observed.update(outcome="unconfirmed")
    records = [observed, observed] if case == "ambiguous" else [observed]
    monkeypatch.setattr(launcher, "verify_broker", verify)
    monkeypatch.setattr(launcher, "telegram_delivery_report", lambda state, since: records)
    predecessor = ("nucbox", 1)
    if case == "survivor":
        result = launcher.observe_delivery(profile, tmp_path, "mac-mini", 2, 120, previous=predecessor)
        assert result["phases"]["final"]["relayMs"] == 95000
        assert verifies == [predecessor, None]
    else:
        with pytest.raises(ValueError):
            launcher.observe_delivery(profile, tmp_path, "mac-mini", 2, 120, previous=predecessor)
        assert verifies == [predecessor]
