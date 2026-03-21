"""Sanity checks for andrea_full_cycle.sh (no live services)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "andrea_full_cycle.sh"
PREREQ = REPO_ROOT / "scripts" / "andrea_wrap_up_prereqs.sh"


class TestAndreaFullCycleScript(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), str(SCRIPT))

    def test_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_script_contains_guardrails(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ANDREA_SYNC_INTERNAL_TOKEN", text)
        self.assertIn("skill-absence?skill=telegram", text)
        self.assertIn("SKIP_KILL_DRILL", text)
        self.assertIn("503", text)


class TestAndreaWrapUpPrereqsScript(unittest.TestCase):
    def test_prereqs_script_exists(self) -> None:
        self.assertTrue(PREREQ.is_file(), str(PREREQ))

    def test_prereqs_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(PREREQ)],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
