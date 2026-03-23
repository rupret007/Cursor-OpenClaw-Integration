"""Unit tests for the Andrea autonomous optimization loop."""

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
from services.andrea_sync.optimizer import (  # noqa: E402
    _try_self_heal_plan_first,
    apply_optimization_proposal,
    build_background_regression_report,
    collect_recent_task_outcomes,
    detect_failure_categories,
    evaluate_autonomy_gate,
    heal_runtime_capability,
    record_regression_report,
    run_optimization_cycle,
)
from services.andrea_sync.schema import CommandType, EventType  # noqa: E402
from services.andrea_sync.store import append_event, connect, migrate, save_experience_run  # noqa: E402


class AndreaSyncOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._prev_db = os.environ.get("ANDREA_SYNC_DB")
        self._prev_kill_switch = os.environ.get("ANDREA_SYNC_KILL_SWITCH")
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
        os.environ.pop("ANDREA_SYNC_KILL_SWITCH", None)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        if self._prev_db is None:
            os.environ.pop("ANDREA_SYNC_DB", None)
        else:
            os.environ["ANDREA_SYNC_DB"] = self._prev_db
        if self._prev_kill_switch is None:
            os.environ.pop("ANDREA_SYNC_KILL_SWITCH", None)
        else:
            os.environ["ANDREA_SYNC_KILL_SWITCH"] = self._prev_kill_switch
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(str(self.db_path) + suf).unlink(missing_ok=True)
        Path(str(self.db_path) + ".kill").unlink(missing_ok=True)

    def _make_overdelegated_task(self) -> str:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "optimizer-source",
                "payload": {"summary": "Is this OpenClaw?"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.USER_MESSAGE,
            {
                "text": "Is this OpenClaw?",
                "routing_text": "Is this OpenClaw?",
                "channel": "telegram",
                "chat_id": 500,
                "message_id": 10,
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "route_reason": "stack_or_tooling_question",
            },
        )
        return tid

    def test_detect_failure_categories_maps_overdelegation(self) -> None:
        categories = detect_failure_categories(
            [
                {
                    "task_id": "tsk_demo",
                    "summary": "Is this OpenClaw?",
                    "outcome": {
                        "terminal_status": "completed",
                        "result_kind": "openclaw_completed",
                        "ux_flags": ["overdelegated_meta_question"],
                    },
                }
            ]
        )
        self.assertEqual(categories[0]["category"], "overdelegation")
        self.assertEqual(categories[0]["count"], 1)

    def test_detect_failure_categories_maps_runtime_leakage_and_blocked_capability(self) -> None:
        categories = detect_failure_categories(
            [
                {
                    "task_id": "tsk_leak",
                    "summary": "I hit a collaboration limit.",
                    "outcome": {
                        "terminal_status": "failed",
                        "result_kind": "openclaw_failed",
                        "ux_flags": [
                            "blocked_capability",
                            "internal_runtime_trace",
                            "runtime_jargon_leaked",
                        ],
                    },
                }
            ]
        )
        category_names = {row["category"] for row in categories}
        self.assertIn("blocked_capability", category_names)
        self.assertIn("runtime_leakage", category_names)

    def test_build_background_regression_report_no_experience_run(self) -> None:
        rep, meta = build_background_regression_report(self.conn, max_age_seconds=3600.0)
        self.assertEqual(rep, {})
        self.assertEqual(meta.get("source"), "none")
        self.assertIn("no_experience_run", str(meta.get("blocked_reason") or ""))

    def test_build_background_regression_report_fresh_passing(self) -> None:
        save_experience_run(
            self.conn,
            {
                "run_id": "exp_bg_fresh",
                "actor": "test",
                "status": "completed",
                "passed": True,
                "summary": "2/2 ok",
                "total_checks": 2,
                "failed_checks": 0,
                "checks": [
                    {"scenario_id": "s1", "passed": True, "score": 95.0},
                    {"scenario_id": "s2", "passed": True, "score": 90.0},
                ],
            },
        )
        rep, meta = build_background_regression_report(self.conn, max_age_seconds=86400.0)
        self.assertTrue(rep.get("passed"))
        self.assertEqual(int(rep.get("total") or 0), 2)
        self.assertEqual(rep.get("command"), "experience_assurance")
        self.assertTrue(meta.get("fresh"))
        self.assertTrue(meta.get("eligible_for_background_repair"))
        vr = meta.get("verification_report") or {}
        self.assertTrue(vr.get("passed"))

    def test_build_background_regression_report_stale_marks_failed(self) -> None:
        save_experience_run(
            self.conn,
            {
                "run_id": "exp_bg_stale",
                "actor": "test",
                "status": "completed",
                "passed": True,
                "summary": "ok",
                "total_checks": 2,
                "checks": [{"scenario_id": "a", "passed": True}],
            },
        )
        self.conn.execute(
            "UPDATE experience_runs SET updated_at = ? WHERE run_id = ?",
            (1.0, "exp_bg_stale"),
        )
        self.conn.commit()
        rep, meta = build_background_regression_report(self.conn, max_age_seconds=60.0)
        self.assertFalse(rep.get("passed"))
        self.assertEqual(rep.get("command"), "experience_assurance_stale")
        self.assertFalse(meta.get("fresh"))
        self.assertFalse(meta.get("eligible_for_background_repair"))

    def test_run_optimization_cycle_includes_autonomy_evidence(self) -> None:
        self._make_overdelegated_task()
        ev = {"source": "experience_assurance", "run_id": "x", "fresh": True, "total_checks": 2}
        result = run_optimization_cycle(
            self.conn,
            limit=10,
            regression_report={"passed": True, "total": 2},
            required_skills=[],
            emit_proposals=True,
            analysis_mode="heuristic",
            autonomy_evidence=ev,
        )
        self.assertEqual(result.get("autonomy_evidence", {}).get("run_id"), "x")

    def test_evaluate_autonomy_gate_requires_regression_and_clear_kill_switch(self) -> None:
        gate = evaluate_autonomy_gate(
            self.conn,
            regression_report={"passed": False, "total": 4},
            required_skills=[],
        )
        self.assertFalse(gate["allowed"])
        self.assertIn("regression_report_missing_or_failed", gate["reasons"])

        handle_command(
            self.conn,
            {
                "command_type": CommandType.KILL_SWITCH_ENGAGE.value,
                "channel": "internal",
                "payload": {"reason": "test"},
            },
        )
        gate = evaluate_autonomy_gate(
            self.conn,
            regression_report={"passed": True, "total": 4},
            required_skills=[],
        )
        self.assertFalse(gate["allowed"])
        self.assertIn("kill_switch_engaged", gate["reasons"])

    def test_run_optimization_cycle_generates_proposals_and_prompt(self) -> None:
        self._make_overdelegated_task()
        result = run_optimization_cycle(
            self.conn,
            limit=10,
            regression_report={"passed": True, "total": 8},
            required_skills=[],
            emit_proposals=True,
            analysis_mode="openclaw_prompt",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(any(row["category"] == "overdelegation" for row in result["findings"]))
        self.assertTrue(any(row["category"] == "overdelegation" for row in result["proposals"]))
        self.assertIn("openclaw_analysis_prompt", result)

        outcomes = collect_recent_task_outcomes(self.conn, limit=10)
        self.assertEqual(outcomes[0]["outcome"]["route_mode"], "delegate")

    def test_run_optimization_cycle_background_skips_when_not_idle(self) -> None:
        self._make_overdelegated_task()
        result = run_optimization_cycle(
            self.conn,
            limit=10,
            regression_report={"passed": True, "total": 8},
            required_skills=[],
            emit_proposals=True,
            analysis_mode="gemini_background",
            repo_path=REPO_ROOT,
            idle_seconds=9999,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "background_not_idle")

    def test_run_optimization_cycle_background_runs_lane_bundle(self) -> None:
        self._make_overdelegated_task()
        with mock.patch(
            "services.andrea_sync.optimizer.evaluate_background_readiness",
            return_value={"ready": True, "active_task_ids": [], "idle_ok": True},
        ), mock.patch(
            "services.andrea_sync.optimizer._run_background_analysis_lanes",
            return_value={
                "planner": {"ok": True, "summary": "Gemini planned the next move."},
                "critiques": [{"ok": True, "summary": "MiniMax challenged the draft."}],
                "budget_usage": {"gemini_runs": 1, "minimax_runs": 1, "openai_runs": 1, "cursor_execution_runs": 1},
                "auto_heal": {"applied": [{"proposal_id": "prop_auto"}], "failed": []},
            },
        ):
            result = run_optimization_cycle(
                self.conn,
                limit=10,
                regression_report={"passed": True, "total": 8},
                required_skills=[],
                emit_proposals=True,
                analysis_mode="gemini_background",
                repo_path=REPO_ROOT,
                auto_apply_ready=True,
                idle_seconds=1,
            )
        self.assertTrue(result["ok"])
        self.assertIn("analysis_lanes", result)
        self.assertEqual(result["analysis_lanes"]["planner"]["summary"], "Gemini planned the next move.")
        self.assertEqual(result["budget_usage"]["gemini_runs"], 1)
        self.assertEqual(len(result["auto_heal"]["applied"]), 1)

    def test_detect_failure_categories_maps_orchestration_and_proactive_failures(self) -> None:
        categories = detect_failure_categories(
            [
                {
                    "task_id": "tsk_orch",
                    "summary": "bad orchestration",
                    "outcome": {
                        "terminal_status": "failed",
                        "result_kind": "openclaw_failed",
                        "ux_flags": [
                            "planner_failure",
                            "critic_missing",
                            "executor_failure",
                            "proactive_delivery_failed",
                        ],
                    },
                }
            ]
        )
        category_names = {row["category"] for row in categories}
        self.assertIn("planner_failure", category_names)
        self.assertIn("critic_failure", category_names)
        self.assertIn("executor_failure", category_names)
        self.assertIn("proactive_delivery", category_names)

    def test_record_regression_report_appends_system_event(self) -> None:
        report = record_regression_report(
            self.conn,
            {"passed": True, "total": 12, "command": "python3 -m unittest"},
            actor="script",
        )
        self.assertTrue(report["passed"])
        events = [row for row in self.conn.execute("SELECT event_type FROM events").fetchall()]
        self.assertTrue(any(str(row["event_type"]) == EventType.REGRESSION_RECORDED.value for row in events))

    def test_apply_optimization_proposal_blocks_disallowed_targets(self) -> None:
        result = apply_optimization_proposal(
            self.conn,
            proposal_payload={
                "proposal_id": "prop_sensitive",
                "title": "Touch env files",
                "category": "runtime_leakage",
                "status": "branch_prep_ready",
                "branch_prep_allowed": True,
                "target_files": [".env", "services/andrea_sync/server.py"],
            },
            repo_path=REPO_ROOT,
            actor="test",
        )
        self.assertFalse(result["ok"])
        self.assertIn("sensitive_target_path", result["error"])

    def test_apply_optimization_proposal_runs_cursor_handoff(self) -> None:
        with (
            mock.patch(
                "services.andrea_sync.optimizer._run_cursor_handoff_prompt",
                return_value={
                    "ok": True,
                    "backend": "cli",
                    "branch": "openclaw/autoheal-prop_ready",
                    "agent_id": "ag_cli",
                    "agent_url": "",
                    "pr_url": "",
                    "status": "FINISHED",
                },
            ),
            mock.patch(
                "services.andrea_sync.optimizer.poll_cursor_agent_until_terminal",
                return_value=("FINISHED", {}),
            ),
            mock.patch(
                "services.andrea_sync.optimizer.verify_cursor_branch_in_isolated_worktree",
                return_value={
                    "ok": True,
                    "passed": True,
                    "verification_report": {"passed": True},
                    "ref_source": "local_branch",
                    "error": "",
                },
            ),
        ):
            result = apply_optimization_proposal(
                self.conn,
                proposal_payload={
                    "proposal_id": "prop_ready",
                    "title": "Tighten routing",
                    "category": "overdelegation",
                    "status": "branch_prep_ready",
                    "branch_prep_allowed": True,
                    "target_files": ["services/andrea_sync/andrea_router.py", "tests/test_andrea_sync.py"],
                    "recommended_action": "Adjust routing and tests.",
                },
                repo_path=REPO_ROOT,
                actor="test",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["proposal_id"], "prop_ready")
        self.assertEqual(result.get("verification_status"), "passed")

    def test_apply_optimization_proposal_fails_when_post_verify_disabled(self) -> None:
        prev = os.environ.get("ANDREA_SELF_HEAL_POST_CURSOR_VERIFY")
        os.environ["ANDREA_SELF_HEAL_POST_CURSOR_VERIFY"] = "0"
        try:
            with (
                mock.patch(
                    "services.andrea_sync.optimizer._run_cursor_handoff_prompt",
                    return_value={
                        "ok": True,
                        "backend": "cli",
                        "branch": "openclaw/autoheal-prop_verify",
                        "agent_id": "ag1",
                        "agent_url": "",
                        "pr_url": "",
                        "status": "FINISHED",
                    },
                ),
                mock.patch(
                    "services.andrea_sync.optimizer.poll_cursor_agent_until_terminal",
                    return_value=("FINISHED", {}),
                ),
            ):
                result = apply_optimization_proposal(
                    self.conn,
                    proposal_payload={
                        "proposal_id": "prop_verify_off",
                        "title": "x",
                        "category": "overdelegation",
                        "status": "branch_prep_ready",
                        "branch_prep_allowed": True,
                        "target_files": ["services/andrea_sync/andrea_router.py"],
                        "recommended_action": "y",
                    },
                    repo_path=REPO_ROOT,
                    actor="test",
                )
            self.assertFalse(result["ok"])
            self.assertIn("self_heal_post_cursor_verify_disabled", result["error"])
        finally:
            if prev is None:
                os.environ.pop("ANDREA_SELF_HEAL_POST_CURSOR_VERIFY", None)
            else:
                os.environ["ANDREA_SELF_HEAL_POST_CURSOR_VERIFY"] = prev

    def test_heal_runtime_capability_installs_missing_dependency(self) -> None:
        before = {
            "ok": True,
            "skill_key": "apple-notes",
            "eligible": False,
            "source": "openclaw-bundled",
            "missing": {"bins": ["memo"], "env": [], "config": [], "os": []},
        }
        after = {
            "ok": True,
            "skill_key": "apple-notes",
            "eligible": True,
            "source": "openclaw-bundled",
            "missing": {"bins": [], "env": [], "config": [], "os": []},
        }
        with mock.patch(
            "services.andrea_sync.optimizer._openclaw_skill_info",
            side_effect=[before, after],
        ), mock.patch(
            "services.andrea_sync.optimizer._install_commands_for_skill",
            return_value=[["brew", "install", "memo"]],
        ), mock.patch(
            "services.andrea_sync.optimizer._repair_missing_config",
            return_value={"ok": True, "changed": False, "actions": [], "unsupported": []},
        ), mock.patch(
            "services.andrea_sync.optimizer._publish_capability_snapshot_direct",
            return_value={"ok": True, "summary": {"ready": 1}},
        ), mock.patch(
            "services.andrea_sync.optimizer._run_subprocess",
            return_value={"ok": True, "argv": ["brew", "install", "memo"], "returncode": 0, "stdout": "", "stderr": ""},
        ):
            result = heal_runtime_capability(self.conn, skill_key="apple-notes", actor="test")
        self.assertTrue(result["ok"])
        self.assertTrue(result["refresh_required"])
        self.assertTrue(any(action.get("kind") == "dependency_install" for action in result["actions"]))

    def test_heal_runtime_capability_repairs_supported_config(self) -> None:
        before = {
            "ok": True,
            "skill_key": "voice-call",
            "eligible": False,
            "source": "openclaw-bundled",
            "missing": {
                "bins": [],
                "env": [],
                "config": ["plugins.entries.voice-call.enabled"],
                "os": [],
            },
        }
        after = {
            "ok": True,
            "skill_key": "voice-call",
            "eligible": True,
            "source": "openclaw-bundled",
            "missing": {"bins": [], "env": [], "config": [], "os": []},
        }
        with mock.patch(
            "services.andrea_sync.optimizer._openclaw_skill_info",
            side_effect=[before, after],
        ), mock.patch(
            "services.andrea_sync.optimizer._repair_missing_config",
            return_value={
                "ok": True,
                "changed": True,
                "actions": [{"kind": "config_repair", "path": "plugins.entries.voice-call.enabled"}],
                "unsupported": [],
            },
        ), mock.patch(
            "services.andrea_sync.optimizer._install_commands_for_skill",
            return_value=[],
        ), mock.patch(
            "services.andrea_sync.optimizer._publish_capability_snapshot_direct",
            return_value={"ok": True, "summary": {"ready": 1}},
        ):
            result = heal_runtime_capability(self.conn, skill_key="voice-call", actor="test")
        self.assertTrue(result["ok"])
        self.assertTrue(result["refresh_required"])
        self.assertTrue(any(action.get("kind") == "config_repair" for action in result["actions"]))

    def test_try_self_heal_plan_first_skips_without_planner_model(self) -> None:
        saved: dict[str, str | None] = {}
        for key in (
            "ANDREA_SELF_HEAL_CURSOR_PLAN_FIRST",
            "ANDREA_CURSOR_PLAN_FIRST_ENABLED",
            "ANDREA_SELF_HEAL_CURSOR_PLANNER_MODEL",
            "ANDREA_CURSOR_PLANNER_MODEL",
        ):
            saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        try:
            self.assertIsNone(
                _try_self_heal_plan_first(
                    repo_path=REPO_ROOT,
                    executor_prompt="fix tests",
                    branch="openclaw/autoheal-unittest",
                    cursor_mode="cli",
                )
            )
            os.environ["ANDREA_SELF_HEAL_CURSOR_PLAN_FIRST"] = "1"
            self.assertIsNone(
                _try_self_heal_plan_first(
                    repo_path=REPO_ROOT,
                    executor_prompt="fix tests",
                    branch="openclaw/autoheal-unittest",
                    cursor_mode="cli",
                )
            )
        finally:
            for key, val in saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

    def test_heal_runtime_capability_command_is_internal_only(self) -> None:
        denied = handle_command(
            self.conn,
            {
                "command_type": CommandType.HEAL_RUNTIME_CAPABILITY.value,
                "channel": "cli",
                "payload": {"skill_key": "bluebubbles"},
            },
        )
        self.assertFalse(denied["ok"])
        with mock.patch(
            "services.andrea_sync.optimizer.heal_runtime_capability",
            return_value={"ok": True, "skill_key": "bluebubbles"},
        ):
            allowed = handle_command(
                self.conn,
                {
                    "command_type": CommandType.HEAL_RUNTIME_CAPABILITY.value,
                    "channel": "internal",
                    "payload": {"skill_key": "bluebubbles"},
                },
            )
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["skill_key"], "bluebubbles")
