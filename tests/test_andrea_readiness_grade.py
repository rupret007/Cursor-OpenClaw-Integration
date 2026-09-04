"""Unit tests for Andrea readiness grade (no subprocess to capabilities)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
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

    def test_readiness_plan_installs_openclaw_binary_before_skills_list(self) -> None:
        plan = self._mod.build_readiness_plan(
            {
                "rows": [
                    {
                        "id": "openclaw:skills_list",
                        "status": "blocked",
                        "critical": True,
                        "notes": "do-not-echo-skills-list-note",
                    },
                    {
                        "id": "binary:openclaw",
                        "status": "blocked",
                        "critical": True,
                        "notes": "do-not-echo-binary-note",
                    },
                ]
            },
            "C",
        )
        self.assertEqual(plan["actions"][0]["id"], "binary:openclaw")
        self.assertIn("Install the required openclaw binary", plan["next_action"])
        self.assertEqual(plan["owner_next_action"], plan["next_action"])
        self.assertEqual(plan["who_acts_first"], self._mod.WHO_ACTS_FIRST_OWNER)
        self.assertNotIn("do-not-echo", str(plan))

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
        self.assertEqual(plan["next_action"], self._mod.RESTORE_MATRIX)
        self.assertEqual(plan["owner_next_action"], self._mod.RESTORE_MATRIX)
        self.assertIn("Do not run autonomous", plan["andrea_next_action"])
        self.assertIn("Stay offline", plan["coding_agent_next_action"])
        self.assertEqual(plan["who_acts_first"], self._mod.WHO_ACTS_FIRST_OWNER)
        self.assertIn("Do not send any live message.", plan["holds"])
        self.assertIn("Keep Private API off.", plan["holds"])
        self.assertIn("coding agent (Bob)", plan["routing"]["coding_agent"])

    def test_grade_a_plan_always_names_andrea_bob_and_owner_next_steps(self) -> None:
        plan = self._mod.build_readiness_plan(
            {
                "rows": [{"id": "binary:python", "status": "ready", "critical": False}],
                "summary": {"blocked": 0, "ready_with_limits": 0},
            },
            "A",
        )
        self.assertTrue(plan["safe_for_autonomous_ops"])
        self.assertEqual(plan["blocker_count"], 0)
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["next_action"], self._mod.GRADE_A_NEXT)
        self.assertEqual(plan["owner_next_action"], self._mod.NO_OWNER_SETUP)
        self.assertEqual(plan["who_acts_first"], self._mod.WHO_ACTS_FIRST_CODING_AGENT)
        self.assertIn("send it / send it now / send now", plan["andrea_next_action"])
        self.assertIn("Continue offline verification", plan["coding_agent_next_action"])
        self.assertNotIn("do-not-echo", str(plan))
        self.assertEqual(plan["holds"], list(self._mod.READINESS_HOLDS))

    def test_grade_b_limits_plan_keeps_optional_lanes_off_limits(self) -> None:
        plan = self._mod.build_readiness_plan(
            {
                "rows": [
                    {"id": "skill:bluebubbles", "status": "ready_with_limits", "critical": False}
                ],
                "summary": {"blocked": 0, "ready_with_limits": 1},
            },
            "B",
        )
        self.assertTrue(plan["safe_for_autonomous_ops"])
        self.assertEqual(plan["next_action"], self._mod.REVIEW_LIMITS)
        self.assertEqual(plan["owner_next_action"], self._mod.REVIEW_LIMITS)
        self.assertEqual(plan["who_acts_first"], self._mod.WHO_ACTS_FIRST_OWNER)
        self.assertIn("verified lanes only", plan["andrea_next_action"])
        self.assertIn("ready-with-limits", plan["coding_agent_next_action"])
        self.assertIn("Do not live-send BlueBubbles.", plan["holds"])

    def test_human_format_makes_actor_next_steps_unmistakable(self) -> None:
        plan = self._mod.build_readiness_plan(
            {
                "rows": [
                    {
                        "id": "skill:cursor_handoff",
                        "status": "blocked",
                        "critical": True,
                        "notes": "must-not-appear",
                    }
                ]
            },
            "C",
        )
        text = self._mod.format_readiness_human(
            {
                "grade": "C",
                "reasons": ["critical_blocked:skill:cursor_handoff"],
                "summary": {"blocked": 1},
                "readiness_plan": plan,
            }
        )
        self.assertTrue(text.startswith(self._mod.OPERATOR_RECAP_START), text)
        recap = self._mod.extract_operator_recap(text)
        self.assertIn("Who acts first: owner", recap)
        self.assertIn("Next action:", recap)
        self.assertIn("Next for Andrea:", recap)
        self.assertIn("Next for the coding agent (Bob):", recap)
        self.assertIn("Next for the owner:", recap)
        self.assertIn("Holds:", recap)
        self.assertLess(text.index("Next for the owner:"), text.index('"blocked": 1'))
        self.assertIn("Routing:", text)
        self.assertNotIn("must-not-appear", text)
        self.assertNotIn("fix blocked rows above", text)

    def test_json_out_matches_stdout_payload_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "readiness.json"
            proc = subprocess.run(
                [
                    "python3",
                    str(GRADE_SCRIPT),
                    "--json",
                    "--json-out",
                    str(output),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            self.assertIn(proc.returncode, {0, 1}, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_doctor_script_leads_with_next_step_contract_not_clipped_table(self) -> None:
        script = DOCTOR_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("head -20", script)
        self.assertNotIn("fix blocked rows above", script)
        self.assertIn("Next for the coding agent (Bob)", script)
        self.assertIn("Private API stays off", script)
        self.assertIn("readiness next-step contract", script)
        self.assertIn("Continuing the offline doctor", script)
        self.assertIn("Operator recap (Andrea / Bob / owner)", script)
        self.assertIn("reprint_operator_recap", script)

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
        self.assertIn("next-step contract", proc.stdout)
        self.assertIn("Bob", proc.stdout)
        self.assertIn("operator-testable", proc.stdout)
        self.assertIn("Grade C", proc.stdout)
        self.assertIn("--receipt", proc.stdout)
        self.assertIn("Grok", proc.stdout)
        self.assertIn("--consume", proc.stdout)
        self.assertIn("audience", proc.stdout)

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

    def test_offline_doctor_completes_and_reprints_andrea_bob_owner_recap(self) -> None:
        """Run the exact operator command and assert the recap is testable."""
        proc = subprocess.run(
            ["bash", str(DOCTOR_SCRIPT), "--offline"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        self.assertIn(proc.returncode, {0, 1}, combined)
        self.assertIn(self._mod.OPERATOR_RECAP_START, proc.stdout)
        self.assertIn(self._mod.OPERATOR_RECAP_END, proc.stdout)
        recap = self._mod.extract_operator_recap(proc.stdout)
        self.assertIn("Who acts first:", recap)
        self.assertIn("Next for Andrea:", recap)
        self.assertIn("Next for the coding agent (Bob):", recap)
        self.assertIn("Next for the owner:", recap)
        self.assertIn("Do not send any live message.", recap)
        self.assertIn("Keep Private API off.", recap)
        self.assertGreaterEqual(proc.stdout.count(self._mod.OPERATOR_RECAP_START), 2)
        self.assertIn(">>> [3/4] Reliability probes (deterministic)", proc.stdout)
        self.assertIn("(Skip: offline mode / SKIP_OPENCLAW_PROBE=1)", proc.stdout)
        self.assertIn("Offline doctor complete.", proc.stdout)
        self.assertIn("Private API stays off", proc.stdout)
        self.assertNotIn("fix blocked rows above", combined)
        self.assertNotIn("openclaw models status --probe", proc.stdout)
        if proc.returncode == 1:
            self.assertIn("Next for the owner", proc.stderr)
            self.assertIn("Continuing the offline doctor", proc.stderr)

    def test_operator_docs_lead_with_offline_doctor_command(self) -> None:
        for rel in (
            "README.md",
            "docs/ANDREA_OPERATIONS_PLAYBOOK.md",
            "docs/ANDREA_READINESS_REPORT.md",
        ):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(doc=rel):
                self.assertRegex(
                    text,
                    r"(?m)^bash scripts/andrea_doctor.sh --offline\s*$",
                )


if __name__ == "__main__":
    unittest.main()
