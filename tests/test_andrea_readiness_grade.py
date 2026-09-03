"""Unit tests for Andrea readiness grade (no subprocess to capabilities)."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GRADE_SCRIPT = REPO_ROOT / "scripts" / "andrea_readiness_grade.py"
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "andrea_doctor.sh"


def _load_grade_module():
    spec = importlib.util.spec_from_file_location("andrea_readiness_grade", str(GRADE_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAndreaReadinessGrade(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = _load_grade_module()

    def test_grade_a_minimal(self) -> None:
        g, reasons = self._mod.grade_from_payload(
            {
                "ok": True,
                "rows": [{"id": "binary:python", "status": "ready", "critical": False}],
                "summary": {"blocked": 0, "ready_with_limits": 0},
            }
        )
        self.assertEqual(g, "A")
        self.assertEqual(reasons, [])

    def test_grade_c_capabilities_failed(self) -> None:
        g, reasons = self._mod.grade_from_payload({"ok": False, "error": "boom"})
        self.assertEqual(g, "C")
        self.assertIn("boom", reasons[0])

    def test_grade_c_missing_rows(self) -> None:
        g, _ = self._mod.grade_from_payload({"ok": True, "summary": {}})
        self.assertEqual(g, "C")

    def test_grade_c_blocked_non_critical(self) -> None:
        g, reasons = self._mod.grade_from_payload(
            {
                "ok": True,
                "rows": [{"id": "x", "status": "blocked", "critical": False}],
                "summary": {"blocked": 1, "ready_with_limits": 0},
            }
        )
        self.assertEqual(g, "C")
        self.assertTrue(any(r.startswith("blocked:") for r in reasons))

    def test_grade_c_critical_blocked(self) -> None:
        g, reasons = self._mod.grade_from_payload(
            {
                "ok": True,
                "rows": [{"id": "cursor:key", "status": "blocked", "critical": True}],
                "summary": {"blocked": 1, "ready_with_limits": 0},
            }
        )
        self.assertEqual(g, "C")
        self.assertTrue(any("critical_blocked" in r for r in reasons))

    def test_grade_b_high_limits(self) -> None:
        thr = self._mod.SOFT_LIMITS_THRESHOLD
        over = thr + 1
        rows = [
            {"id": f"opt:{n}", "status": "ready_with_limits", "critical": False}
            for n in range(over)
        ]
        g, reasons = self._mod.grade_from_payload(
            {
                "ok": True,
                "rows": rows,
                "summary": {"blocked": 0, "ready_with_limits": over},
            }
        )
        self.assertEqual(g, "B")
        self.assertTrue(any("ready_with_limits_count" in r for r in reasons))

    def test_grade_b_github_degraded(self) -> None:
        g, reasons = self._mod.grade_from_payload(
            {
                "ok": True,
                "rows": [
                    {"id": "github:auth", "status": "ready_with_limits", "critical": False},
                    {"id": "binary:python", "status": "ready", "critical": False},
                ],
                "summary": {"blocked": 0, "ready_with_limits": 1},
            }
        )
        self.assertEqual(g, "B")
        self.assertIn("github:auth_degraded", reasons)

    def test_readiness_plan_prioritizes_critical_blockers_without_echoing_notes(self) -> None:
        plan = self._mod.build_readiness_plan(
            {
                "rows": [
                    {
                        "id": "skill:add-minimax-provider",
                        "status": "blocked",
                        "critical": True,
                        "notes": "must-not-become-the-first-action",
                    },
                    {
                        "id": "binary:optional-tool",
                        "status": "blocked",
                        "critical": False,
                        "notes": "do-not-echo-secret-value",
                    },
                    {
                        "id": "skill:cursor_handoff",
                        "status": "blocked",
                        "critical": True,
                        "notes": "also-do-not-echo",
                    },
                ]
            },
            "C",
        )
        self.assertFalse(plan["safe_for_autonomous_ops"])
        self.assertEqual(plan["blocker_count"], 3)
        self.assertEqual(plan["actions"][0]["id"], "skill:cursor_handoff")
        self.assertIn("docs/OPENCLAW_SKILL.md", plan["next_action"])
        self.assertNotIn("do-not-echo", str(plan))

    def test_readiness_plan_fails_closed_on_invalid_capability_rows(self) -> None:
        plan = self._mod.build_readiness_plan({"rows": "not-a-list"}, "C")
        self.assertFalse(plan["safe_for_autonomous_ops"])
        self.assertEqual(plan["blocker_count"], 1)
        self.assertEqual(plan["actions"][0]["id"], "capabilities:payload")

    def test_doctor_help_names_offline_mode_without_running_checks(self) -> None:
        proc = subprocess.run(
            ["bash", str(DOCTOR_SCRIPT), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--offline", proc.stdout)

    def test_doctor_rejects_unknown_options(self) -> None:
        proc = subprocess.run(
            ["bash", str(DOCTOR_SCRIPT), "--not-real"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Unknown option", proc.stderr)


if __name__ == "__main__":
    unittest.main()
