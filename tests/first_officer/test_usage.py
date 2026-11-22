import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
HANDOFF_SCRIPT = ROOT / "skills/cursor_handoff/scripts/cursor_handoff.py"
spec = importlib.util.spec_from_file_location("usage_track", ROOT / "first-officer/usage/track.py")
track = importlib.util.module_from_spec(spec)
spec.loader.exec_module(track)

NOW = 1_753_000_000
CST_RESET = 1_763_710_000
CDT_RESET = 1_753_700_000


def codex_result(session=40, weekly=70, session_reset=CST_RESET, weekly_reset=CDT_RESET):
    return {"accountId": "fixture", "ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": session, "resetsAt": session_reset, "windowDurationMins": 300},
        "secondary": {"usedPercent": weekly, "resetsAt": weekly_reset, "windowDurationMins": 10080},
        "credits": {"hasCredits": False, "unlimited": False},
    }}


def claude_headers(session="0.4", weekly="0.7", status="allowed",
                   session_reset=CST_RESET, weekly_reset=CDT_RESET):
    return {"Anthropic-RateLimit-Unified-Status": status,
            "anthropic-ratelimit-unified-5h-utilization": session,
            "anthropic-ratelimit-unified-5h-reset": str(session_reset),
            "anthropic-ratelimit-unified-7d-utilization": weekly,
            "anthropic-ratelimit-unified-7d-reset": str(weekly_reset)}


def cursor_reading(spend=400, *, now=NOW, end=CDT_RESET):
    return {
        "observed_at": now,
        "period_start": now - 86_400,
        "period_end": end,
        "included_spend": spend,
        "included_limit": 1000,
        "billing": {
            "has_credits": False,
            "unlimited": False,
            "on_demand_enabled": False,
            "usage_billing_enabled": False,
            "auto_top_up_enabled": False,
        },
    }


def write_cursor_reading(path, spend=400):
    now = int(time.time())
    path.write_text(json.dumps(cursor_reading(spend, now=now, end=now + 3600)))
    return path


def repo(tmp_path, branch="codex-candace/fo-finish-20261120"):
    path = tmp_path / "checkout"
    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    run("init", "-q", "-b", branch)
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    (path / "README.md").write_text("fixture\n")
    run("add", "README.md")
    run("commit", "-q", "-m", "fixture")
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return path, branch, head


def test_resets_render_in_central_time_across_the_daylight_change():
    assert track.chicago_reset(CST_RESET) == "November 21, 2025 at 1:26:40 a.m. CST"
    assert track.chicago_reset(CDT_RESET) == "July 28, 2025 at 5:53:20 a.m. CDT"
    assert track.chicago_reset(1_753_678_800) == "July 28, 2025 at 12:00:00 a.m. CDT"
    assert track.chicago_reset(1_753_722_000) == "July 28, 2025 at 12:00:00 p.m. CDT"


def test_codex_reader_reports_the_five_hour_and_weekly_windows():
    rows = track.codex_windows(codex_result(), "fixture", NOW)
    assert [row["window"] for row in rows] == ["5h", "weekly"]
    assert [row["used"] for row in rows] == [40, 70]
    assert rows[0]["reset_text"].endswith("CST") and rows[1]["reset_text"].endswith("CDT")
    with pytest.raises(ValueError, match="account changed"):
        track.codex_windows(codex_result(), "another-account", NOW)
    with pytest.raises(ValueError, match="Incomplete"):
        track.codex_windows(codex_result(session_reset=NOW - 1), "fixture", NOW)


def test_claude_reader_converts_utilization_and_honors_a_rejected_status():
    rows = track.claude_windows(claude_headers(), NOW)
    assert [(row["window"], row["used"]) for row in rows] == [("session", 40.0), ("weekly", 70.0)]
    assert rows[0]["reset_text"] == "November 21, 2025 at 1:26:40 a.m. CST"
    rejected = track.claude_windows(claude_headers(session="0.1", status="rejected"), NOW)
    assert [row["used"] for row in rejected] == [100.0, 100.0]
    assert track.claude_windows(claude_headers(session="1.4"), NOW)[0]["used"] == 100.0


def test_claude_reader_fails_closed_on_unusable_headers():
    for headers, message in (
        ({}, "Unknown Claude rate-limit status"),
        (claude_headers(status="throttled"), "Unknown Claude rate-limit status"),
        ({k: v for k, v in claude_headers().items() if "7d-reset" not in k}, "Incomplete Claude weekly"),
        (claude_headers(session="none"), "Incomplete Claude session"),
        (claude_headers(session="-0.2"), "Unusable claude session utilization"),
        ({**claude_headers(), "anthropic-ratelimit-unified-5h-reset": str(NOW - 1)}, "stale"),
        ({**claude_headers(), "ANTHROPIC-RATELIMIT-UNIFIED-STATUS": "allowed"}, "repeat a name"),
    ):
        with pytest.raises(ValueError, match=message):
            track.claude_windows(headers, NOW)


def test_warning_starts_at_ninety_percent_and_stopping_at_ninety_seven():
    def used(value):
        return track.assess(track.claude_windows(claude_headers(session=str(value / 100)), NOW))

    assert used(89.9)["warned"] == [] and used(89.9)["stop"] is False
    assert [row["window"] for row in used(90)["warned"]] == ["session"]
    assert used(90)["stop"] is False
    assert used(96.9)["stop"] is False
    assert used(97)["stop"] is True
    assert used(89.9)["unread"] == [("codex", "5h"), ("codex", "weekly"), ("cursor", "billing-period")]
    with pytest.raises(ValueError, match="no usage windows"):
        track.assess([])
    with pytest.raises(ValueError, match="thresholds"):
        track.assess(track.claude_windows(claude_headers(), NOW), 95, 90)


def test_usage_overrides_may_only_be_stricter_than_the_defaults(tmp_path, capsys):
    windows = track.claude_windows(claude_headers(session="0.98"), NOW)
    with pytest.raises(ValueError, match="stricter"):
        track.assess(windows, 100, 100)
    assert track.assess(windows, 80, 90)["stop"] is True
    ahead = int(time.time()) + 3600
    claude = tmp_path / "claude.json"
    claude.write_text(json.dumps(claude_headers(
        session="0.98", session_reset=ahead, weekly_reset=ahead + 86_400)))
    cursor = write_cursor_reading(tmp_path / "cursor.json")
    args = ["--claude", str(claude), "--cursor", str(cursor)]
    assert track.main(args + ["--warn-percent", "100", "--stop-percent", "100"]) == 1
    assert "stricter" in capsys.readouterr().err
    assert track.main(args + ["--warn-percent", "80", "--stop-percent", "90"]) == track.STOPPED


def test_all_three_providers_report_together_with_central_resets():
    report = track.assess(track.codex_windows(codex_result(session=92), "fixture", NOW) +
                          track.claude_windows(claude_headers(weekly="0.95"), NOW) +
                          track.cursor_windows(cursor_reading(), NOW))
    assert report["unread"] == [] and report["stop"] is False
    assert [(row["provider"], row["window"]) for row in report["warned"]] == [
        ("claude", "weekly"), ("codex", "5h")]
    text = track.report_text(report)
    assert "Codex 5h window at 92% used. It resets November 21, 2025 at 1:26:40 a.m. CST." in text
    assert "Claude weekly window at 95% used. It resets July 28, 2025 at 5:53:20 a.m. CDT." in text
    assert "Cursor billing-period window at 40% used. It resets July 28, 2025 at 5:53:20 a.m. CDT." in text
    assert "At or above the 90% warning threshold." in text


def test_warning_writes_and_commits_the_handoff_without_pushing(tmp_path):
    path, branch, head = repo(tmp_path)
    report = track.assess(track.codex_windows(codex_result(session=93), "fixture", NOW))
    commit = track.commit_handoff(path, report, branch, head, "Step 2", "Start step 3",
                                  ["LiteLLM fixture rerun is pending"])

    body = (path / "HANDOFF.md").read_text()
    assert f"Branch `{branch}` is at `{head}`." in body
    assert "Codex 5h window at 93% used." in body and "a.m. CST." in body
    assert "Step in progress: Step 2" in body and "Next: Start step 3" in body
    assert "Open failure: LiteLLM fixture rerun is pending" in body
    assert "Nothing was pushed, merged or activated by this handoff." in body

    log = subprocess.run(["git", "-C", str(path), "log", "--format=%H %s", "-2"],
                         check=True, capture_output=True, text=True).stdout.splitlines()
    assert log[0] == f"{commit} Record First Officer usage warning and resume point"
    assert log[1].split()[0] == head
    assert subprocess.run(["git", "-C", str(path), "status", "--porcelain"], check=True,
                          capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "-C", str(path), "remote"], check=True,
                          capture_output=True, text=True).stdout == ""


def test_handoff_commits_only_itself_and_never_repeats_an_unchanged_document(tmp_path):
    path, branch, head = repo(tmp_path)
    report = track.assess(track.codex_windows(codex_result(session=93), "fixture", NOW))
    first = track.commit_handoff(path, report, branch, head, "Step 2", "Start step 3")

    (path / "README.md").write_text("unrelated lane edit\n")
    (path / "scratch.txt").write_text("unrelated new file\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    assert track.commit_handoff(path, report, branch, head, "Step 2", "Start step 3") is None

    later = track.assess(track.codex_windows(codex_result(session=98), "fixture", NOW))
    second = track.commit_handoff(path, later, branch, head, "Step 2", "Start step 3")
    assert second != first
    changed = subprocess.run(["git", "-C", str(path), "show", "--name-only", "--format=", second],
                             check=True, capture_output=True, text=True).stdout.split()
    assert changed == ["HANDOFF.md"]
    assert (path / "README.md").read_text() == "unrelated lane edit\n"
    assert "98% used" in (path / "HANDOFF.md").read_text()


def test_handoff_refuses_a_protected_branch_a_moved_head_and_a_leaked_credential(tmp_path):
    report = track.assess(track.codex_windows(codex_result(session=93), "fixture", NOW))
    path, branch, head = repo(tmp_path, branch="main")
    with pytest.raises(ValueError, match="protected branch"):
        track.commit_handoff(path, report, branch, head, "Step 2", "Start step 3")
    assert not (path / "HANDOFF.md").exists()

    lane, branch, head = repo(tmp_path / "lane")
    with pytest.raises(ValueError, match="not on the branch"):
        track.commit_handoff(lane, report, "codex/other", head, "Step 2", "Next")
    with pytest.raises(ValueError, match="not in this branch's history"):
        track.commit_handoff(lane, report, branch, "0" * 40, "Step 2", "Next")
    with pytest.raises(ValueError, match="not in this branch's history"):
        track.commit_handoff(lane, report, branch, head[:7], "Step 2", "Next")
    with pytest.raises(ValueError, match="credential or address"):
        track.commit_handoff(lane, report, branch, head, "Step 2", "Use sk-abcd1234efgh5678")
    with pytest.raises(ValueError, match="nonempty text"):
        track.commit_handoff(lane, report, branch, head, "Step 2", "  ")
    assert not (lane / "HANDOFF.md").exists()


def test_handoff_refuses_scanner_credential_shapes(tmp_path, capsys):
    """A failure field cannot carry a scanner credential into HANDOFF.md or the command output."""
    report = track.assess(track.codex_windows(codex_result(session=93), "fixture", NOW))
    lane, branch, head = repo(tmp_path / "lane")
    shapes = (
        "github" + "_pat_" + "A" * 22,
        "Bearer " + "a" * 24,
        "BEARER " + "C" * 20,
        "AS" + "IA" + "E" * 16,
        "Bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN"[:-1] + "M"),
        "bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN" + "S"),
        "Bearer " + "ab-cd.ef_ghijklm",
        "BEARER " + ("aa_bb" + "-cc.dd/ee+ff~gg"),
    )
    for secret in shapes:
        with pytest.raises(ValueError, match="credential or address") as caught:
            track.commit_handoff(lane, report, branch, head, "Step 2", "Next", [f"saw {secret}"])
        assert secret not in str(caught.value)
        assert not (lane / "HANDOFF.md").exists()

    allowed, allowed_branch, allowed_head = repo(tmp_path / "fixture-ok")
    fixture_failure = "saw Bearer " + "ANDREA_SYNC_INTERNAL_TOKEN"
    track.commit_handoff(allowed, report, allowed_branch, allowed_head, "Step 2", "Next",
                         [fixture_failure])
    assert "ANDREA_SYNC_INTERNAL_TOKEN" in (allowed / "HANDOFF.md").read_text()
    assert ("ANDREA_SYNC_INTERNAL_TOKEN"[:-1] + "M") not in (allowed / "HANDOFF.md").read_text()

    path, _branch, _head = repo(tmp_path / "cli")
    ahead = int(time.time()) + 3600
    codex = tmp_path / "codex.json"
    codex.write_text(json.dumps(codex_result(session=93, session_reset=ahead, weekly_reset=ahead)))
    cursor = write_cursor_reading(tmp_path / "cursor.json")
    leaked = "github" + "_pat_" + "b" * 22
    assert track.main(["--codex", str(codex), "--codex-account", "fixture", "--cursor", str(cursor), "--repo", str(path),
                       "--failure", f"saw {leaked}"]) == 1
    captured = capsys.readouterr()
    assert leaked not in captured.err + captured.out
    assert "credential or address" in captured.err
    assert not (path / "HANDOFF.md").exists()


def test_command_line_reports_quietly_and_hands_off_once_a_window_warns(tmp_path, capsys):
    path, branch, head = repo(tmp_path)
    codex = tmp_path / "codex.json"
    claude = tmp_path / "claude.json"
    # The command line reads the wall clock, so these fixtures must reset in the real future.
    ahead = int(time.time()) + 3600

    def live_codex(**fields):
        return json.dumps(codex_result(session_reset=ahead, weekly_reset=ahead + 86_400, **fields))

    claude.write_text(json.dumps(claude_headers(session_reset=ahead, weekly_reset=ahead + 86_400)))
    codex.write_text(live_codex())
    cursor = write_cursor_reading(tmp_path / "cursor.json")
    arguments = ["--codex", str(codex), "--codex-account", "fixture", "--claude", str(claude),
                 "--cursor", str(cursor), "--repo", str(path), "--step", "Step 2", "--next", "Start step 3"]
    assert track.main(arguments) == track.OK
    assert "Codex 5h window at 40% used." in capsys.readouterr().out
    assert not (path / "HANDOFF.md").exists()

    codex.write_text(live_codex(weekly=91))
    assert track.main(arguments) == track.WARNED
    assert "Handoff committed as" in capsys.readouterr().out
    assert "Codex weekly window at 91% used." in (path / "HANDOFF.md").read_text()

    codex.write_text(live_codex(weekly=97))
    assert track.main(arguments) == track.STOPPED
    assert "Stop after the handoff." in (path / "HANDOFF.md").read_text()

    assert track.main(["--codex", str(codex), "--codex-account", "someone-else", "--cursor", str(cursor)]) == 1
    assert "Usage tracking failed" in capsys.readouterr().err


def test_command_line_redacts_a_credential_from_its_failure_text(tmp_path, capsys):
    path, branch, head = repo(tmp_path)
    ahead = int(time.time()) + 3600
    codex = tmp_path / "codex.json"
    codex.write_text(json.dumps(codex_result(session=93, session_reset=ahead, weekly_reset=ahead)))
    cursor = write_cursor_reading(tmp_path / "cursor.json")
    arguments = ["--codex", str(codex), "--codex-account", "fixture", "--cursor", str(cursor), "--repo", str(path)]
    assert track.main(arguments + ["--next", "rotate sk-abcd1234efgh5678"]) == 1
    captured = capsys.readouterr()
    assert "sk-abcd1234efgh5678" not in captured.err + captured.out
    assert "credential or address" in captured.err
    assert not (path / "HANDOFF.md").exists()

    same_shape = "Bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN"[:-1] + "M")
    mixed = "bearer " + "ab-cd.ef_ghijklm"
    for failure in (same_shape, mixed):
        assert track.main(arguments + ["--failure", "saw " + failure]) == 1
        captured = capsys.readouterr()
        assert failure not in captured.err + captured.out
        assert "credential or address" in captured.err
        assert not (path / "HANDOFF.md").exists()

    assert track.main(["--codex", str(tmp_path / "sk-abcd1234efgh5678/absent.json"), "--cursor", str(cursor)]) == 1
    captured = capsys.readouterr().err
    assert "sk-abcd1234efgh5678" not in captured and "[redacted]/absent.json" in captured


@pytest.mark.parametrize(("end", "reset_text"), [
    (CST_RESET, "November 21, 2025 at 1:26:40 a.m. CST"),
    (CDT_RESET, "July 28, 2025 at 5:53:20 a.m. CDT"),
])
def test_cursor_uses_included_spend_and_the_billing_period_end(end, reset_text):
    reading = cursor_reading(375, end=end)
    reading["included_limit"] = 500
    assert track.cursor_windows(reading, NOW) == [{
        "provider": "cursor", "window": "billing-period", "used": 75.0,
        "reset": end, "reset_text": reset_text,
    }]


@pytest.mark.parametrize("fields", [
    {"included_limit": 0}, {"included_limit": True}, {"included_limit": "1000"},
    {"included_spend": -1}, {"included_spend": float("nan")}, {"included_spend": True},
    {"period_end": NOW}, {"period_end": "2025-07-28"}, {"period_start": NOW + 1},
    {"observed_at": NOW - 91}, {"observed_at": NOW + 1},
])
def test_cursor_rejects_unusable_amounts_and_noncurrent_periods(fields):
    with pytest.raises(ValueError):
        track.cursor_windows(cursor_reading() | fields, NOW)


@pytest.mark.parametrize("field", [
    "has_credits", "unlimited", "on_demand_enabled", "usage_billing_enabled", "auto_top_up_enabled",
])
def test_cursor_timer_refuses_each_paid_extra_even_at_zero_included_usage(field, tmp_path, capsys):
    cursor = write_cursor_reading(tmp_path / "cursor.json", spend=0)
    reading = json.loads(cursor.read_text())
    for unsafe in (True, None, 0, "false"):
        reading["billing"][field] = unsafe
        cursor.write_text(json.dumps(reading))
        assert track.main(["--cursor", str(cursor)]) == 1
        captured = capsys.readouterr()
        assert "The cursor billing-period window was not read." in captured.err
        assert "Cursor paid extras are not confirmed disabled" in captured.err
        assert not captured.out
    del reading["billing"][field]
    cursor.write_text(json.dumps(reading))
    assert track.main(["--cursor", str(cursor)]) == 1
    assert "Cursor paid extras are not confirmed disabled" in capsys.readouterr().err


def test_cursor_timer_refuses_unread_input_even_with_healthy_codex_and_claude(tmp_path, capsys):
    path, _, head = repo(tmp_path)
    ahead = int(time.time()) + 3600
    codex = tmp_path / "codex.json"
    codex.write_text(json.dumps(codex_result(session_reset=ahead, weekly_reset=ahead)))
    claude = tmp_path / "claude.json"
    claude.write_text(json.dumps(claude_headers(session_reset=ahead, weekly_reset=ahead)))
    cursor = tmp_path / "cursor.json"
    args = ["--codex", str(codex), "--codex-account", "fixture", "--claude", str(claude), "--repo", str(path)]

    for cursor_args in ([], ["--cursor", str(cursor)]):
        assert track.main(args + cursor_args) == 1
        assert "The cursor billing-period window was not read." in capsys.readouterr().err

    no_session = {"status": "unread", "reason": "dashboard_token_required"}
    token_value = "private-session-fixture"
    valid = cursor_reading(0, now=ahead - 3600, end=ahead)
    for unusable in (
        "{",
        " " * (256 * 1024 + 1),
        json.dumps(no_session),
        json.dumps(valid | {"dashboard_token": token_value}),
        json.dumps(valid | {"billing": valid["billing"] | {"new_billing_mode": "metered"}}),
        json.dumps(valid | {"observed_at": ahead - 3600 - 91}),
        json.dumps(valid).replace('"has_credits": false', '"has_credits": true, "has_credits": false'),
    ):
        cursor.write_text(unusable)
        assert track.main(args + ["--cursor", str(cursor)]) == 1
        captured = capsys.readouterr()
        assert "The cursor billing-period window was not read." in captured.err
        assert token_value not in captured.err and not captured.out
        assert cursor.read_text() == unusable

    assert track.git(path, "rev-parse", "HEAD").stdout.strip() == head
    assert not (path / "HANDOFF.md").exists()


@pytest.mark.parametrize(("spend", "exit_code"), [(0, 0), (899, 0), (900, 3), (969, 3), (970, 4), (1000, 4)])
def test_cursor_tracker_thresholds_and_handoff_commit(spend, exit_code, tmp_path, capsys):
    path, branch, head = repo(tmp_path)
    cursor = write_cursor_reading(tmp_path / "cursor.json", spend)
    (path / "README.md").write_text("unrelated staged edit\n")
    track.git(path, "add", "README.md")

    assert track.main(["--cursor", str(cursor), "--repo", str(path),
                       "--step", "Cursor fallback", "--next", "Resume after billing reset"]) == exit_code
    report = capsys.readouterr().out
    assert f"Cursor billing-period window at {spend / 10:g}% used." in report
    if exit_code:
        handoff = (path / "HANDOFF.md").read_text()
        assert f"Branch `{branch}` is at `{head}`." in handoff
        assert f"Cursor billing-period window at {spend / 10:g}% used." in handoff
        assert ("Stop after the handoff." in handoff) is (exit_code == 4)
        assert "Handoff committed as" in report
        assert track.git(path, "show", "--name-only", "--format=").stdout.split() == ["HANDOFF.md"]
    else:
        assert not (path / "HANDOFF.md").exists()
        assert track.git(path, "rev-parse", "HEAD").stdout.strip() == head
    assert track.git(path, "diff", "--cached", "--name-only").stdout.split() == ["README.md"]
    assert not track.git(path, "remote").stdout


@pytest.mark.parametrize("mode", ["cli", "auto"])
def test_cursor_handoff_gates_the_real_fallback_wrapper_before_invoking_agent(mode, tmp_path):
    agent = tmp_path / "agent"
    calls = tmp_path / "calls.jsonl"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CURSOR_TEST_CALLS'], 'a') as log:\n"
        "    log.write(json.dumps([sys.argv[1:], 'CURSOR_API_KEY' in os.environ]) + '\\n')\n"
        "sys.exit(int(os.environ.get('CURSOR_TEST_EXIT', '0')))\n"
    )
    agent.chmod(0o700)
    cursor = tmp_path / "plan.json"
    env = {
        "PATH": os.environ["PATH"],
        "CURSOR_PLAN_READING": str(cursor),
        "CURSOR_CLI_BIN": str(agent),
        "CURSOR_TEST_CALLS": str(calls),
        "CURSOR_API_KEY": "fixture_cloud_key",
        "CURSOR_BASE_URL": "http://localhost:1",
    }
    arguments = [sys.executable, str(HANDOFF_SCRIPT), "--mode", mode, "--json", "--api-retries", "0",
                 "--repo", str(tmp_path), "--prompt", "Review fixture", "--model", "claude-fixture"]

    def handoff():
        result = subprocess.run(arguments, cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        return result.returncode, json.loads(result.stdout)

    paid_extras = [(899, {field: True}) for field in cursor_reading()["billing"]]
    for spend, billing in [(None, {}), *paid_extras, (900, {}), (970, {})]:
        if spend is not None:
            write_cursor_reading(cursor, spend)
            reading = json.loads(cursor.read_text())
            reading["billing"].update(billing)
            cursor.write_text(json.dumps(reading))
        code, result = handoff()
        assert code != 0 and result["ok"] is False and result["backend"] == "cli" and result["exit_code"] == 5
        assert "Cursor fallback requires a readable plan below the warning threshold" in result["stderr"]
        assert not calls.exists()

    write_cursor_reading(cursor, 899)
    code, result = handoff()
    assert code == 0 and result["ok"] is True and result["model"] == "composer-2.5"
    (argv, had_key), = (json.loads(line) for line in calls.read_text().splitlines())
    assert argv[:2] == ["--model", "composer-2.5"] and "Review fixture" in argv[2]
    assert len(argv) == 3 and had_key is False

    env["CURSOR_TEST_EXIT"] = "2"
    code, result = handoff()
    assert code != 0 and result["ok"] is False
    assert len(calls.read_text().splitlines()) == 2
