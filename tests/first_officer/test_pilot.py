import importlib.util
import json
from pathlib import Path
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_pilot", ROOT / "first-officer/pilot/rehearse.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)

NOW = 1_763_700_000


def test_rehearsal_fails_over_a_missed_check_in_and_rejects_the_old_epoch(tmp_path, capsys):
    root = tmp_path / "pilot"
    lines = pilot.rehearse(root, 12, NOW)
    assert lines == [
        "Armed takeover ON while captain held epoch 12.",
        "Watchdog moved the lease to first-officer at epoch 13.",
        "Granted integration-ci at epoch 13.",
        "One lane accepted for pull 33.",
        "A second submission was a duplicate.",
        "Pause rejected a new lane.",
        "A missed check-in returned the lease to captain at epoch 14.",
        "An old-epoch submit was rejected.",
    ]
    lease = json.loads((root / "lease.json").read_text(encoding="utf-8"))
    assert lease["holder"] == "captain" and lease["epoch"] == 14 and lease["takeover"] == "OFF"
    assert lease["grant"] is None and lease["lane"]["state"] == "interrupted"
    assert (root / "lease.json").stat().st_mode & 0o777 == 0o600
    assert pilot.main(["rehearse", "--root", str(tmp_path / "again"), "--epoch", "12", "--now", str(NOW)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == lines
    with pytest.raises(ValueError, match="staged copy"):
        pilot.rehearse(Path.home() / "work/lanes/openclaw-backup2-20261118-1340/staged", 12, NOW)
    with pytest.raises(ValueError, match="fresh"):
        stale = tmp_path / "stale"
        pilot.begin(stale, 12, NOW)
        pilot.arm(stale, 12, NOW + pilot.FRESH_S + 1)


def test_handback_returns_captain_and_two_ticks_advance_one_epoch(tmp_path):
    root = tmp_path / "pilot"
    pilot.begin(root, 12, NOW)
    pilot.arm(root, 12, NOW)
    moved = pilot.tick(root, NOW)
    assert pilot.grant(root, moved["epoch"], NOW) == "grant"
    assert pilot.submit(root, moved["epoch"], NOW) == "running"
    assert pilot.handback(root, moved["epoch"], NOW) == "handback"
    returned = pilot.tick(root, NOW)
    assert returned == {"epoch": 14, "holder": "captain", "result": "handback"}
    lease = json.loads((root / "lease.json").read_text(encoding="utf-8"))
    assert lease["lane"]["state"] == "running" and lease["takeover"] == "OFF"
    assert pilot.submit(root, moved["epoch"], NOW) == "stale"

    raced = tmp_path / "raced"
    pilot.begin(raced, 12, NOW)
    pilot.arm(raced, 12, NOW)
    pilot.tick(raced, NOW)
    results = []

    def once():
        results.append(pilot.tick(raced, NOW + pilot.FRESH_S + 1))

    threads = [threading.Thread(target=once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert sorted(result["epoch"] for result in results) == [14, 14]
    assert sorted(result["result"] for result in results) == ["missed-check-in", "unchanged"]


def test_checklist_matches_the_live_steps_and_does_not_open_a_lease(capsys):
    assert pilot.main(["checklist"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert printed == list(pilot.CHECKLIST)
    assert "Send no message" in printed[-1]
    actions = (ROOT / "JEFF-ACTIONS.md").read_text(encoding="utf-8")
    assert all(line in actions for line in pilot.CHECKLIST)
    text = Path(pilot.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in text and "systemctl" not in text
