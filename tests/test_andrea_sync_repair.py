"""Unit tests for Andrea's incident-driven repair pipeline."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.repair_orchestrator import run_incident_repair_cycle  # noqa: E402
from services.andrea_sync.repair_policy import patch_guardrails  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    SYSTEM_TASK_ID,
    connect,
    get_incident,
    get_latest_repair_plan,
    list_repair_attempts,
    load_events_for_task,
    migrate,
)


class AndreaSyncRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(str(self.db_path) + suf).unlink(missing_ok=True)

    def _failing_verification_report(self) -> dict[str, object]:
        return {
            "passed": False,
            "checks": [
                {
                    "check_id": "unit",
                    "label": "Unit Tests",
                    "command": "python3 -m unittest discover -p 'test_*.py'",
                    "passed": False,
                    "required": True,
                    "output_excerpt": (
                        "FAIL: test_route_direct\n"
                        "AssertionError: expected direct reply\n"
                        "services/andrea_sync/server.py"
                    ),
                }
            ],
            "summary": "Failed checks: Unit Tests",
        }

    def test_patch_guardrails_reject_sensitive_and_large_patch(self) -> None:
        verdict = patch_guardrails(
            {
                "files_touched": [
                    ".env",
                    "services/andrea_sync/server.py",
                    "services/andrea_sync/schema.py",
                    "services/andrea_sync/bus.py",
                ],
                "diff": "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-token=old\n+token=new\n",
                "reasoning_summary": "Touch too many files including env.",
            },
            attempt_number=1,
        )
        self.assertFalse(verdict["allowed"])
        self.assertTrue(any("sensitive_target" in reason for reason in verdict["reasons"]))
        self.assertTrue(any("too_many_files" in reason for reason in verdict["reasons"]))

    def test_run_incident_repair_cycle_resolves_with_primary_patch(self) -> None:
        worktree_dir = tempfile.mkdtemp()
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "A narrow routing branch regressed.",
                        "affected_files": ["services/andrea_sync/server.py"],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-2 files in services/andrea_sync/",
                        "confidence": 0.82,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Restore the direct routing branch for lightweight questions.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.88,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            return_value={
                "ok": True,
                "branch": "repair/inc-demo-primary",
                "worktree_path": worktree_dir,
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            return_value={
                "passed": True,
                "checks": [{"label": "Unit Tests", "passed": True}],
                "summary": "All enabled required verification checks passed.",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.commit_worktree_if_clean",
            return_value={"ok": True, "commit_sha": "abc123"},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed"]},
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["resolved"])
        incident = result["incident"]
        self.assertEqual(incident["status"], "resolved")
        attempts = list_repair_attempts(self.conn, incident["incident_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertEqual(attempts[0]["prompt_version"], "v1")
        stored = get_incident(self.conn, incident["incident_id"])
        self.assertEqual(stored["status"], "resolved")
        self.assertEqual(stored["metadata"]["triage_prompt_version"], "v1")
        event_types = [et for _seq, _ts, et, _payload in load_events_for_task(self.conn, SYSTEM_TASK_ID)]
        self.assertIn(EventType.INCIDENT_RECORDED.value, event_types)
        self.assertIn(EventType.INCIDENT_RESOLVED.value, event_types)

    def test_run_incident_repair_cycle_escalates_after_failed_attempts(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {
                    "ok": True,
                    "branch": "repair/inc-demo-a1",
                    "worktree_path": worktree_one,
                },
                {
                    "ok": True,
                    "branch": "repair/inc-demo-a2",
                    "worktree_path": worktree_two,
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-demo.json",
                "markdown_path": "/tmp/inc-demo.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-demo-cursor",
                "backend": "cli",
                "agent_id": "cursor-123",
                "agent_url": "https://cursor.com/agents/cursor-123",
                "pr_url": "",
                "status": "submitted",
                "prompt": "Use Cursor for the coordinated multi-file implementation.",
            },
        ), mock.patch.dict(os.environ, {"ANDREA_REPAIR_POST_CURSOR_VERIFY": "0"}):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["resolved"])
        self.assertEqual(result["status"], "cursor_handoff_ready")
        self.assertTrue(result["cursor_handoff"]["ok"])
        self.assertEqual(result["report_path"], "/tmp/inc-demo.json")
        incident_id = result["incident"]["incident_id"]
        attempts = list_repair_attempts(self.conn, incident_id)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(row["status"] == "failed" for row in attempts))
        plan = get_latest_repair_plan(self.conn, incident_id)
        self.assertEqual(plan["root_cause"], "The direct and delegated orchestration metadata are out of sync.")
        self.assertEqual(plan["prompt_version"], "v1")
        conductor = (plan.get("metadata") or {}).get("conductor") or {}
        self.assertEqual(conductor.get("preferred_executor"), "cursor_handoff")
        self.assertIn("heavy_repair_plan", conductor.get("escalation_reasons") or [])
        self.assertTrue(conductor.get("effective_cursor_execute"))
        self.assertTrue(conductor.get("cursor_execute_requested"))
        stored = get_incident(self.conn, incident_id)
        im_conductor = (stored.get("metadata") or {}).get("conductor") or {}
        self.assertEqual(im_conductor.get("preferred_executor"), conductor.get("preferred_executor"))
        self.assertEqual(im_conductor.get("escalation_reasons"), conductor.get("escalation_reasons"))
        handoff = im_conductor.get("handoff") or {}
        self.assertTrue(handoff.get("ok"))
        self.assertEqual(handoff.get("branch"), "repair/inc-demo-cursor")
        self.assertIn("cursor.com", handoff.get("agent_url") or "")
        outcome = im_conductor.get("outcome") or {}
        self.assertEqual(outcome.get("submission_status"), "succeeded")
        self.assertEqual(outcome.get("verification_status"), "skipped")
        event_types = [et for _seq, _ts, et, _payload in load_events_for_task(self.conn, SYSTEM_TASK_ID)]
        self.assertIn(EventType.REPAIR_HANDOFF_RECORDED.value, event_types)
        self.assertIn(EventType.INCIDENT_ESCALATED.value, event_types)

    def test_run_incident_repair_cycle_cursor_post_verify_can_resolve(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {
                    "ok": True,
                    "branch": "repair/inc-demo-a1",
                    "worktree_path": worktree_one,
                },
                {
                    "ok": True,
                    "branch": "repair/inc-demo-a2",
                    "worktree_path": worktree_two,
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-demo-postv.json",
                "markdown_path": "/tmp/inc-demo-postv.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-demo-cursor",
                "backend": "cli",
                "agent_id": "cursor-123",
                "agent_url": "https://cursor.com/agents/cursor-123",
                "pr_url": "",
                "status": "FINISHED",
                "prompt": "Use Cursor for the coordinated multi-file implementation.",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.verify_cursor_branch_in_isolated_worktree",
            return_value={
                "ok": True,
                "passed": True,
                "verification_report": {
                    "passed": True,
                    "checks": [{"label": "Unit Tests", "passed": True}],
                    "summary": "ok",
                },
                "ref_source": "local",
                "error": "",
            },
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["cursor_handoff"]["ok"])
        post = result.get("post_cursor_verification") or {}
        self.assertTrue(post.get("passed"))
        ce = result.get("conductor_escalation") or {}
        self.assertEqual((ce.get("outcome") or {}).get("verification_status"), "passed")
        incident_id = result["incident"]["incident_id"]
        stored = get_incident(self.conn, incident_id)
        self.assertEqual(stored["status"], "resolved")
        event_types = [et for _seq, _ts, et, _payload in load_events_for_task(self.conn, SYSTEM_TASK_ID)]
        self.assertIn(EventType.INCIDENT_RESOLVED.value, event_types)
        self.assertNotIn(EventType.INCIDENT_ESCALATED.value, event_types)

    def test_run_incident_repair_cycle_auto_cursor_heavy_env_triggers_handoff(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()
        with mock.patch.dict(
            os.environ,
            {"ANDREA_REPAIR_AUTO_CURSOR_HEAVY": "1", "ANDREA_REPAIR_POST_CURSOR_VERIFY": "0"},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {"ok": True, "branch": "repair/inc-demo-a1", "worktree_path": worktree_one},
                {"ok": True, "branch": "repair/inc-demo-a2", "worktree_path": worktree_two},
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={"json_path": "/tmp/inc-demo-auto.json", "markdown_path": "/tmp/inc-demo-auto.md"},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-demo-cursor-auto",
                "backend": "cli",
                "agent_id": "cursor-auto",
                "agent_url": "https://cursor.com/agents/cursor-auto",
                "pr_url": "",
                "status": "submitted",
                "prompt": "auto",
            },
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=False,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["resolved"])
        self.assertEqual(result["status"], "cursor_handoff_ready")
        ce = result.get("conductor_escalation") or {}
        self.assertTrue(ce.get("auto_cursor_heavy"))
        self.assertFalse(ce.get("cursor_execute_requested"))
        self.assertTrue(ce.get("effective_cursor_execute"))
        incident_id = result["incident"]["incident_id"]
        stored = get_incident(self.conn, incident_id)
        im_conductor = (stored.get("metadata") or {}).get("conductor") or {}
        self.assertEqual(im_conductor.get("preferred_executor"), ce.get("preferred_executor"))
        self.assertTrue(im_conductor.get("auto_cursor_heavy"))
        handoff = im_conductor.get("handoff") or {}
        self.assertTrue(handoff.get("ok"))
        self.assertEqual(handoff.get("branch"), "repair/inc-demo-cursor-auto")

    def test_repair_api_handoff_polls_until_finished_then_resolves(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {"ok": True, "branch": "repair/inc-api-a1", "worktree_path": worktree_one},
                {"ok": True, "branch": "repair/inc-api-a2", "worktree_path": worktree_two},
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-api-poll.json",
                "markdown_path": "/tmp/inc-api-poll.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-api-cursor",
                "backend": "api",
                "agent_id": "ag-api-1",
                "agent_url": "",
                "pr_url": "",
                "status": "RUNNING",
                "prompt": "cursor",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.poll_cursor_agent_until_terminal",
            return_value=(
                "FINISHED",
                {"target": {"url": "https://cursor.example/agents/ag-api-1", "prUrl": ""}},
            ),
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.verify_cursor_branch_in_isolated_worktree",
            return_value={
                "ok": True,
                "passed": True,
                "verification_report": {
                    "passed": True,
                    "checks": [{"label": "Unit Tests", "passed": True}],
                    "summary": "ok",
                },
                "ref_source": "local",
                "error": "",
            },
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertTrue(result["resolved"])
        ch = result.get("cursor_handoff") or {}
        self.assertEqual(ch.get("status"), "FINISHED")
        self.assertIn("cursor.example", str(ch.get("agent_url") or ""))
        self.assertEqual((result.get("conductor_escalation") or {}).get("outcome", {}).get("verification_status"), "passed")

    def test_repair_api_handoff_non_terminal_after_poll_skips_verify(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()

        def _never_verify(*_a: object, **_k: object) -> dict[str, object]:
            raise AssertionError("verify_cursor_branch_in_isolated_worktree should not run")

        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {"ok": True, "branch": "repair/inc-nt-a1", "worktree_path": worktree_one},
                {"ok": True, "branch": "repair/inc-nt-a2", "worktree_path": worktree_two},
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-nt.json",
                "markdown_path": "/tmp/inc-nt.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-nt-cursor",
                "backend": "api",
                "agent_id": "ag-nt",
                "agent_url": "https://cursor.example/agents/ag-nt",
                "pr_url": "",
                "status": "RUNNING",
                "prompt": "cursor",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.poll_cursor_agent_until_terminal",
            return_value=("RUNNING", {}),
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.verify_cursor_branch_in_isolated_worktree",
            side_effect=_never_verify,
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertFalse(result["resolved"])
        out = (result.get("conductor_escalation") or {}).get("outcome") or {}
        self.assertEqual(out.get("verification_status"), "not_attempted")
        self.assertEqual(out.get("next_action"), "monitor_cursor_or_verify_manually")
        self.assertEqual(result["status"], "cursor_handoff_ready")

    def test_repair_api_handoff_terminal_failed_skips_verify(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()

        def _never_verify(*_a: object, **_k: object) -> dict[str, object]:
            raise AssertionError("verify_cursor_branch_in_isolated_worktree should not run")

        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {"ok": True, "branch": "repair/inc-fail-a1", "worktree_path": worktree_one},
                {"ok": True, "branch": "repair/inc-fail-a2", "worktree_path": worktree_two},
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-fail.json",
                "markdown_path": "/tmp/inc-fail.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_cursor_repair_handoff",
            return_value={
                "ok": True,
                "branch": "repair/inc-fail-cursor",
                "backend": "api",
                "agent_id": "ag-fail",
                "agent_url": "",
                "pr_url": "",
                "status": "RUNNING",
                "prompt": "cursor",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.poll_cursor_agent_until_terminal",
            return_value=("FAILED", {"target": {"url": "https://cursor.example/agents/ag-fail"}}),
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.verify_cursor_branch_in_isolated_worktree",
            side_effect=_never_verify,
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertFalse(result["resolved"])
        out = (result.get("conductor_escalation") or {}).get("outcome") or {}
        self.assertEqual(out.get("verification_status"), "not_attempted")
        self.assertEqual(out.get("next_action"), "human_review_cursor_failed")
        self.assertEqual(result["status"], "human_review_required")
        ch = result.get("cursor_handoff") or {}
        self.assertEqual(ch.get("status"), "FAILED")

    def test_repair_plan_first_fallback_recorded_in_conductor(self) -> None:
        worktree_one = tempfile.mkdtemp()
        worktree_two = tempfile.mkdtemp()
        import json as _json

        handoff_payload = _json.dumps(
            {
                "ok": True,
                "branch": "repair/inc-pf-fb",
                "backend": "cli",
                "agent_id": "c-fb",
                "agent_url": "https://cursor.com/agents/c-fb",
                "pr_url": "",
                "status": "FINISHED",
            }
        )
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_REPAIR_CURSOR_PLAN_FIRST": "1",
                "ANDREA_REPAIR_CURSOR_PLANNER_MODEL": "planner-test",
            },
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "The routing and follow-through logic diverged.",
                        "affected_files": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "1-3 files in services/andrea_sync/",
                        "confidence": 0.8,
                        "safe_to_auto_attempt": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4 mini",
                    "payload": {
                        "reasoning_summary": "Attempt a narrow direct-route fix.",
                        "files_touched": ["services/andrea_sync/server.py"],
                        "diff": (
                            "diff --git a/services/andrea_sync/server.py b/services/andrea_sync/server.py\n"
                            "--- a/services/andrea_sync/server.py\n"
                            "+++ b/services/andrea_sync/server.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.81,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "minimax",
                    "requested_label": "MiniMax M2.7",
                    "payload": {
                        "reasoning_summary": "Try a slightly broader fix after critiquing the first attempt.",
                        "files_touched": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                        ],
                        "diff": (
                            "diff --git a/services/andrea_sync/schema.py b/services/andrea_sync/schema.py\n"
                            "--- a/services/andrea_sync/schema.py\n"
                            "+++ b/services/andrea_sync/schema.py\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "tests_expected": ["Unit Tests"],
                        "confidence": 0.77,
                        "safe_to_apply": True,
                        "test_change_reason": "",
                        "critique_of_previous_attempt": "The first patch was too narrow.",
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The direct and delegated orchestration metadata are out of sync.",
                        "steps": [
                            "Align routing decision storage with final execution metadata.",
                            "Update regression coverage for the direct path.",
                        ],
                        "files_to_modify": [
                            "services/andrea_sync/server.py",
                            "services/andrea_sync/schema.py",
                            "tests/test_andrea_sync.py",
                        ],
                        "risks": ["The fix spans routing and projection behavior."],
                        "verification_plan": ["Unit Tests", "HTTP Tests"],
                        "stop_conditions": ["Stop if the repair expands beyond three core files."],
                        "handoff_summary": "Use Cursor for the coordinated multi-file implementation.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.create_sandbox_worktree",
            side_effect=[
                {"ok": True, "branch": "repair/inc-pf-a1", "worktree_path": worktree_one},
                {"ok": True, "branch": "repair/inc-pf-a2", "worktree_path": worktree_two},
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.apply_unified_diff",
            return_value={"ok": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_verification_suite",
            side_effect=[
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
                {
                    "passed": False,
                    "checks": [{"label": "Unit Tests", "passed": False}],
                    "summary": "Failed checks: Unit Tests",
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.cleanup_worktree",
            return_value={"ok": True, "actions": ["worktree_removed", "branch_deleted"]},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-pf.json",
                "markdown_path": "/tmp/inc-pf.md",
            },
        ), mock.patch(
            "services.andrea_sync.repair_executor._try_repair_cursor_plan_first_handoff",
            return_value=(None, "plan_unusable"),
        ), mock.patch(
            "services.andrea_sync.repair_executor._run_subprocess",
            return_value={"ok": True, "stdout": handoff_payload, "stderr": ""},
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
                cursor_execute=True,
            )

        self.assertTrue(result["ok"])
        ch = result.get("cursor_handoff") or {}
        self.assertEqual(ch.get("cursor_strategy"), "single_pass_fallback")
        self.assertEqual(ch.get("plan_first_fallback_reason"), "plan_unusable")
        incident_id = result["incident"]["incident_id"]
        stored = get_incident(self.conn, incident_id)
        handoff_meta = (stored.get("metadata") or {}).get("conductor", {}).get("handoff") or {}
        self.assertEqual(handoff_meta.get("plan_first_fallback_reason"), "plan_unusable")

    def test_run_incident_repair_cycle_triage_can_require_human_review(self) -> None:
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.main_worktree_clean",
            return_value={"ok": True, "clean": True},
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.run_role_json",
            side_effect=[
                {
                    "ok": True,
                    "requested_family": "gemini",
                    "requested_label": "Gemini Flash Lite",
                    "payload": {
                        "classification": "code_bug",
                        "probable_root_cause": "This needs a broader coordinated repair.",
                        "affected_files": ["services/andrea_sync/server.py"],
                        "failing_tests": ["FAIL: test_route_direct"],
                        "recommended_repair_scope": "needs broader review",
                        "confidence": 0.62,
                        "safe_to_auto_attempt": True,
                        "needs_human_review": True,
                    },
                },
                {
                    "ok": True,
                    "requested_family": "openai",
                    "requested_label": "GPT 5.4",
                    "payload": {
                        "root_cause": "The repair spans more than one safe lightweight patch.",
                        "steps": ["Inspect routing metadata precedence."],
                        "files_to_modify": ["services/andrea_sync/server.py"],
                        "risks": ["Could affect direct-vs-delegate behavior."],
                        "verification_plan": ["Unit Tests"],
                        "stop_conditions": ["Stop if routing rules expand."],
                        "handoff_summary": "Use a broader coordinated repair path.",
                    },
                },
            ],
        ), mock.patch(
            "services.andrea_sync.repair_orchestrator.write_repair_artifacts",
            return_value={
                "json_path": "/tmp/inc-human.json",
                "markdown_path": "/tmp/inc-human.md",
            },
        ):
            result = run_incident_repair_cycle(
                self.conn,
                repo_path=REPO_ROOT,
                actor="test",
                verification_report=self._failing_verification_report(),
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["resolved"])
        self.assertEqual(result["status"], "human_review_required")
        self.assertFalse(result["attempts"])
        self.assertFalse(result["guard"]["allowed"])
        self.assertIn("triage_requires_human_review", result["guard"]["reasons"])
        incident = get_incident(self.conn, result["incident"]["incident_id"])
        self.assertEqual(incident["status"], "human_review_required")

    def test_run_incident_repair_command_is_internal_only(self) -> None:
        denied = handle_command(
            self.conn,
            {
                "command_type": CommandType.RUN_INCIDENT_REPAIR.value,
                "channel": "cli",
                "payload": {},
            },
        )
        self.assertFalse(denied["ok"])
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.run_incident_repair_cycle",
            return_value={"ok": True, "resolved": False, "incident": {"incident_id": "inc_demo"}},
        ):
            allowed = handle_command(
                self.conn,
                {
                    "command_type": CommandType.RUN_INCIDENT_REPAIR.value,
                    "channel": "internal",
                    "payload": {},
                },
            )
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["incident"]["incident_id"], "inc_demo")

    def test_run_incident_repair_command_passes_extended_payloads(self) -> None:
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.run_incident_repair_cycle",
            return_value={"ok": True, "resolved": False, "incident": {"incident_id": "inc_demo"}},
        ) as repair_cycle:
            allowed = handle_command(
                self.conn,
                {
                    "command_type": CommandType.RUN_INCIDENT_REPAIR.value,
                    "channel": "internal",
                    "payload": {
                        "incident_id": "inc_saved",
                        "runtime_error": {"summary": "boom"},
                        "health_failure": {"service_name": "andrea_sync"},
                        "log_alert": {"summary": "log spike"},
                    },
                },
            )
        self.assertTrue(allowed["ok"])
        _, kwargs = repair_cycle.call_args
        self.assertEqual(kwargs["incident_id"], "inc_saved")
        self.assertEqual(kwargs["runtime_error"]["summary"], "boom")
        self.assertEqual(kwargs["health_failure"]["service_name"], "andrea_sync")
        self.assertEqual(kwargs["log_alert"]["summary"], "log spike")
