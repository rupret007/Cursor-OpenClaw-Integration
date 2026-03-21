"""Tests for Andrea capability baseline (no live network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
