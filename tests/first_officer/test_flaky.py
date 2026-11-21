import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_flaky", ROOT / "first-officer/qa/flaky.py")
flaky = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flaky)

TODAY = date(2026, 11, 20)


def attempt(status, retry=0, message=""):
    result = {"status": status, "retry": retry, "errors": [], "attachments": []}
    if message:
        result["errors"] = [{"message": message}]
    if status == "failed":
        result["attachments"] = [{"name": "trace", "path": "/tmp/secret-trace.zip"}]
    return result


def report(*tests):
    specs = []
    for title, project, results, status in tests:
        specs.append({"title": title, "ok": status == "expected", "tests": [{
            "projectName": project, "status": status, "results": results}]})
    return {"suites": [{"title": "", "file": "tests/lane.spec.js", "specs": specs}]}


def entry(title, **extra):
    row = {"project": "chromium", "file": "tests/lane.spec.js", "title": [title],
           "owner": "jeff", "issue": "FO-12", "reason": "Known race in the fixture.",
           "addedOn": "2026-11-20", "expiresOn": "2026-12-04"}
    row.update(extra)
    return row


def test_producer_keeps_a_failed_attempt_after_the_retry_passes():
    events = flaky.produce(report(
        ("stays green", "chromium", [attempt("passed")], "expected"),
        ("blinks", "chromium", [attempt("failed", message="sk-live-secretvalue boom"),
                                attempt("passed", retry=1)], "expected"),
    ))
    blinks = events["tests"][1]
    assert blinks["outcome"] == "flaky"
    assert [item["status"] for item in blinks["attempts"]] == ["failed", "passed"]
    assert blinks["attempts"][0]["error"] == "[redacted] boom"
    assert blinks["attempts"][0]["trace"] is True
    assert "/tmp/" not in json.dumps(events)
    assert "sk-live" not in json.dumps(events)


def test_clean_run_is_green_and_every_other_outcome_is_not():
    clean = flaky.produce(report(("stays green", "chromium", [attempt("passed")], "expected")))
    assert flaky.evaluate(clean, [], TODAY) == {
        "ok": True, "passing": 1, "nonpassing": 0, "quarantined": [], "failures": [],
        "flakes": [], "skipped": [], "interrupted": [], "expired": [], "orphaned": []}

    flake = flaky.produce(report(("blinks", "chromium",
                                  [attempt("failed"), attempt("passed", retry=1)], "flaky")))
    flaky_gate = flaky.evaluate(flake, [], TODAY)
    assert flaky_gate["ok"] is False and flaky_gate["flakes"][0]["attempts"] == ["failed", "passed"]

    failed = flaky.produce(report(("breaks", "chromium", [attempt("failed")], "unexpected")))
    assert flaky.evaluate(failed, [], TODAY)["ok"] is False
    skipped = flaky.produce(report(("absent", "chromium", [attempt("skipped")], "skipped")))
    assert flaky.evaluate(skipped, [], TODAY)["skipped"] == [
        "chromium::tests/lane.spec.js::absent"]


def test_quarantine_stays_visible_and_cannot_make_the_gate_green():
    failed = flaky.produce(report(("breaks", "chromium", [attempt("failed")], "unexpected")))
    gate = flaky.evaluate(failed, flaky.load_quarantine({"entries": [entry("breaks")]}), TODAY)
    assert gate["ok"] is False
    assert gate["passing"] == 0 and gate["nonpassing"] == 1
    assert gate["quarantined"] == [{"id": "chromium::tests/lane.spec.js::breaks",
                                    "outcome": "unexpected", "attempts": ["failed"]}]
    assert gate["failures"] == []

    clean = flaky.produce(report(("stays green", "chromium", [attempt("passed")], "expected")))
    hidden = flaky.evaluate(clean, flaky.load_quarantine({"entries": [entry("stays green")]}), TODAY)
    assert hidden["ok"] is False and hidden["passing"] == 0
    assert hidden["quarantined"][0]["outcome"] == "expected"


def test_expired_and_orphaned_quarantine_remain_failures():
    failed = flaky.produce(report(("breaks", "chromium", [attempt("failed")], "unexpected")))
    expired = entry("breaks", addedOn="2026-11-01", expiresOn="2026-11-15")
    gate = flaky.evaluate(failed, flaky.load_quarantine({"entries": [expired]}), TODAY)
    assert gate["ok"] is False
    assert gate["expired"][0]["outcome"] == "unexpected"
    assert gate["quarantined"] == [] and gate["passing"] == 0

    clean = flaky.produce(report(("stays green", "chromium", [attempt("passed")], "expected")))
    orphan = flaky.evaluate(clean, flaky.load_quarantine({"entries": [entry("missing")]}), TODAY)
    assert orphan["ok"] is False and orphan["passing"] == 1
    assert orphan["orphaned"] == ["chromium::tests/lane.spec.js::missing"]


def test_quarantine_rejects_a_long_window_a_secret_and_a_bad_name():
    for raw, message in (
        (entry("breaks", expiresOn="2026-12-05"), "14 days"),
        (entry("breaks", reason="token sk-live-secretvalue"), "plain reason"),
        (entry("breaks", owner=""), "owner"),
        ({**entry("breaks"), "note": "extra"}, "incomplete"),
    ):
        with pytest.raises(ValueError, match=message):
            flaky.load_quarantine({"entries": [raw]})


def test_a_hand_edited_pass_cannot_erase_a_failed_attempt():
    events = flaky.produce(report(("blinks", "chromium",
                                  [attempt("failed"), attempt("passed", retry=1)], "flaky")))
    events["tests"][0]["outcome"] = "expected"
    reread = flaky.produce_events(events)
    assert reread["tests"][0]["outcome"] == "flaky"
    assert flaky.evaluate(reread, [], TODAY)["ok"] is False


def test_cli_exits_green_only_when_nothing_is_quarantined_or_failing(tmp_path, capsys):
    report_path = tmp_path / "results.json"
    events_path = tmp_path / "flaky-events.json"
    quarantine_path = tmp_path / "quarantine.json"
    summary_path = tmp_path / "summary.json"
    report_path.write_text(json.dumps(report(("stays green", "chromium", [attempt("passed")], "expected"))))
    quarantine_path.write_text(json.dumps({"entries": []}))
    assert flaky.main(["produce", str(report_path), str(events_path)]) == 0
    assert flaky.main(["evaluate", str(events_path), str(quarantine_path), str(summary_path)]) == 0
    assert json.loads(summary_path.read_text())["ok"] is True

    quarantine_path.write_text(json.dumps({"entries": [entry("stays green")]}))
    assert flaky.main(["evaluate", str(events_path), str(quarantine_path), str(summary_path)]) == 1
    summary = json.loads(summary_path.read_text())
    assert summary["ok"] is False and summary["quarantined"]
    assert "Quarantined 1." in capsys.readouterr().out
