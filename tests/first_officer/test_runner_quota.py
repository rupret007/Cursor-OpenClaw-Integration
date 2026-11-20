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
