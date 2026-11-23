import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


path = Path(__file__).resolve().parents[2] / "first-officer/segment1/runner-quota.py"
spec = importlib.util.spec_from_file_location("runner_quota", path)
quota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quota)


def test_only_current_runner_subscription_and_included_quota_can_admit():
    result = {"accountId": "fixture", "ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": 40, "resetsAt": 900, "windowDurationMins": 300},
        "secondary": {"usedPercent": 70, "resetsAt": 1000, "windowDurationMins": 10080},
        "credits": {"hasCredits": False, "unlimited": False},
    }}
    current = quota.quota_windows(result, "fixture", 800)
    assert current == {"session": {"used": 40, "reset": 900}, "weekly": {"used": 70, "reset": 1000}, "allowed": True}
    with pytest.raises(ValueError, match="account changed"):
        quota.quota_windows(result, "someone-else", 800)
    with pytest.raises(ValueError, match="Incomplete"):
        quota.quota_windows(result, "fixture", 950)
    result["rateLimits"]["credits"]["hasCredits"] = True
    assert quota.quota_windows(result, "fixture", 800)["allowed"] is False


def test_stdio_quota_probe_initializes_then_calls_account_read_only():
    simulator = """
import json, sys
for expected in ('initialize', 'initialized', 'account/rateLimits/read'):
    request = json.loads(sys.stdin.readline())
    assert request['method'] == expected
    if 'id' in request:
        print(json.dumps({'id': request['id'], 'result': {'method': expected}}), flush=True)
"""
    with subprocess.Popen([sys.executable, "-c", simulator], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        assert quota.quota_rpc(proc) == {"method": "account/rateLimits/read"}
        assert proc.wait(timeout=1) == 0


def test_standalone_quota_uses_only_the_selected_local_cli(tmp_path):
    local_cli = tmp_path / "codex"
    simulator = """
import json, sys, time
assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]
for expected in ("initialize", "initialized", "account/rateLimits/read"):
    request = json.loads(sys.stdin.readline())
    assert request["method"] == expected
    if "id" in request:
        now = int(time.time())
        result = {"accountId": "fixture", "rateLimits": {
          "primary": {"usedPercent": 12, "resetsAt": now + 3000, "windowDurationMins": 300},
          "secondary": {"usedPercent": 40, "resetsAt": now + 6000, "windowDurationMins": 10080},
        }} if expected == "account/rateLimits/read" else {}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
"""
    local_cli.write_text(f"#!{sys.executable}\n" + simulator)
    local_cli.chmod(0o700)
    result = quota.read_local_quota(local_cli)
    assert result["session"]["used"] == 12
    assert result["weekly"]["used"] == 40
    assert result["observed"] < result["session"]["reset"]
    assert "accountId" not in result
    with pytest.raises(ValueError, match="standalone Codex CLI is unavailable"):
        quota.read_local_quota("codex")


@pytest.mark.parametrize(("account_type", "session", "credits", "eligible"), [
    ("chatgpt", True, {"hasCredits": False, "unlimited": False}, True),
    ("chatgpt", False, {"hasCredits": False, "unlimited": False}, False),
    ("chatgpt", True, None, False),
    ("chatgpt", True, {"hasCredits": True, "unlimited": False}, False),
    ("apiKey", True, {"hasCredits": False, "unlimited": False}, None),
])
def test_coding_preflight_requires_current_subscription_and_funding_evidence(
        tmp_path, monkeypatch, account_type, session, credits, eligible):
    local_cli = tmp_path / "codex"
    account = {"type": account_type, "email": "private@example.invalid"}
    snapshot = {"secondary": {"usedPercent": 40, "resetsAt": 4_000_000_000, "windowDurationMins": 10080},
                "credits": credits}
    if session:
        snapshot["primary"] = {"usedPercent": 12, "resetsAt": 4_000_000_000, "windowDurationMins": 300}
    simulator = f"""
import json, os, sys
assert "OPENAI_API_KEY" not in os.environ and "CODEX_HOME" not in os.environ
for expected in ("initialize", "initialized", "account/read", "account/rateLimits/read"):
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line)
    assert request["method"] == expected
    result = {{}}
    if expected == "account/read":
        assert request["params"] == {{"refreshToken": False}}
        result = {{"account": {account!r}, "requiresOpenaiAuth": True}}
    elif expected == "account/rateLimits/read":
        result = {{"rateLimits": {snapshot!r}}}
    if "id" in request:
        print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
"""
    local_cli.write_text(f"#!{sys.executable}\n" + simulator)
    local_cli.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-profile"))
    if eligible is None:
        with pytest.raises(ValueError, match="subscription sign-in"):
            quota.read_coding_allowance(local_cli)
    else:
        result = quota.read_coding_allowance(local_cli)
        assert result["eligible"] is eligible
        assert "private@example.invalid" not in str(result)


def test_standalone_accepts_a_weekly_only_subscription():
    response = {"rateLimits": {
        "primary": {"usedPercent": 30, "resetsAt": 1_900_000_000, "windowDurationMins": 10080},
        "secondary": None,
    }}
    result = quota.local_quota_windows(response, 1_800_000_000)
    assert result == {"weekly": {"used": 30, "reset": 1_900_000_000}}
