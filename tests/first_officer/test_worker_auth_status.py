from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


status = runpy.run_path(str(Path(__file__).resolve().parents[2] / "first-officer/workers/auth_status.py"))


@pytest.mark.parametrize(("provider", "stdout", "code", "expected"), [
    ("claude", '{"loggedIn":true,"authMethod":"claude.ai","email":"private@example.invalid"}', 0, "subscription-sign-in"),
    ("claude", '{"loggedIn":true,"authMethod":"api_key"}', 0, "other-sign-in"),
    ("claude", '{"loggedIn":false}', 1, "not-signed-in"),
    ("claude", "unrecognized output", 0, "inconclusive"),
    ("agent", "Logged in as private@example.invalid", 0, "signed-in"),
    ("agent", "Not authenticated", 1, "not-signed-in"),
    ("agent", "Authenticated as private@example.invalid\nNot logged in", 0, "not-signed-in"),
    ("agent", "Unknown", 0, "inconclusive"),
])
def test_worker_probes_use_only_status_and_hide_account_and_api_keys(
        tmp_path, monkeypatch, provider, stdout, code, expected):
    binary = tmp_path / provider
    binary.touch(mode=0o700)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    monkeypatch.setenv("CURSOR_API_KEY", "fixture-only")
    function = status["claude_subscription_status" if provider == "claude" else "cursor_sign_in_status"]

    def run(argv, **options):
        args = ["auth", "status"] if provider == "claude" else ["status"]
        assert argv == [str(binary), *args]
        assert "ANTHROPIC_API_KEY" not in options["env"]
        assert "CURSOR_API_KEY" not in options["env"]
        return SimpleNamespace(stdout=stdout, stderr="", returncode=code)

    assert function(binary, run=run) == expected
