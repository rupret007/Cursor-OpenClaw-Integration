"""Tests for Andrea capability baseline (no live network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
CAP_SCRIPT = REPO_ROOT / "scripts" / "andrea_capabilities.py"


class TestAndreaCapabilities(unittest.TestCase):
    def test_script_json_exit_zero(self) -> None:
        env = {**os.environ, "ANDREA_REPO_ROOT": str(REPO_ROOT)}
        for k in (
            "CURSOR_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
        ):
            env.pop(k, None)
        env["CURSOR_API_KEY"] = ""
        proc = subprocess.run(
            [sys.executable, str(CAP_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data.get("ok"))
        self.assertIn("rows", data)
        ids = {r["id"] for r in data["rows"]}
        self.assertIn("binary:python", ids)
        self.assertIn("cursor:diagnose", ids)

    def test_skill_match_helper(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        blob = "cursor_handoff ready\n  github\ngh-issues"
        rows = ac._skill_rows(blob)
        by_detail = {r.detail: r.status for r in rows}
        self.assertEqual(by_detail.get("cursor_handoff"), "ready")
        self.assertEqual(by_detail.get("github"), "ready")
        self.assertEqual(by_detail.get("gh-issues"), "ready")

    def test_parse_openclaw_skill_states_table(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        snippet = (
            "┌───────────┬─────────────────────────┬──────────┬──────────┐\n"
            "│ ✓ ready   │ 📦 cursor_handoff       │ desc     │ src      │\n"
            "│ ✗ missing │ 📝 apple-notes          │ desc     │ bundled  │\n"
        )
        states = ac._parse_openclaw_skill_states(snippet)
        self.assertEqual(states.get("cursor_handoff"), "ready")
        self.assertEqual(states.get("apple-notes"), "missing")

    def test_expected_skills_include_hybrid_wave1(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        for name in (
            "apple-notes",
            "apple-reminders",
            "things-mac",
            "gog",
            "summarize",
            "session-logs",
        ):
            self.assertIn(name, ac.EXPECTED_OPENCLAW_SKILLS)

    def test_acpx_blocked_when_acp_router_ready_but_binary_missing(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        snippet = "│ ✓ ready   │ 📦 acp-router           │ desc     │ bundled  │\n"
        with mock.patch.object(ac, "_which", side_effect=lambda name: name != "acpx"):
            rows = ac._acp_support_rows(snippet)
        by_id = {r.id: r for r in rows}
        self.assertEqual(by_id["skill:acp-router"].status, "ready")
        self.assertEqual(by_id["acp_tool:acpx"].status, "blocked")
        self.assertIn("npm install -g acpx", by_id["acp_tool:acpx"].notes)

    def test_acpx_ready_when_binary_present(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        snippet = "│ ✓ ready   │ 📦 acp-router           │ desc     │ bundled  │\n"
        with mock.patch.object(ac, "_which", return_value=True):
            rows = ac._acp_support_rows(snippet)
        by_id = {r.id: r for r in rows}
        self.assertEqual(by_id["skill:acp-router"].status, "ready")
        self.assertEqual(by_id["acp_tool:acpx"].status, "ready")

    def test_gh_auth_state_reads_dotenv_without_process_env(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        saved = {k: os.environ.pop(k) for k in ("GH_TOKEN", "GITHUB_TOKEN") if k in os.environ}
        try:
            with mock.patch.object(ac, "_which", return_value=True), mock.patch.object(
                ac,
                "_run_capture",
                return_value=(1, "", "not logged in"),
            ):
                st, note = ac._gh_auth_state(
                    {"GH_TOKEN": "ghp_test_dummy"},
                    {},
                )
            self.assertEqual(st, "ready_with_limits")
            self.assertIn("repo .env", note)
        finally:
            os.environ.update(saved)

    def test_github_token_present_false_when_empty(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import andrea_capabilities as ac  # noqa: E402

        saved = {k: os.environ.pop(k) for k in ("GH_TOKEN", "GITHUB_TOKEN") if k in os.environ}
        try:
            self.assertFalse(ac._github_token_present({}, {}))
        finally:
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
