"""Tests for Andrea platform blueprint modules (goals, router, memory, recovery, workflows)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.andrea_sync.bus import handle_command
from services.andrea_sync.dashboard import _build_blueprint_platform_summary
from services.andrea_sync.experience_assurance import blueprint_platform_health
from services.andrea_sync.failure_classifier import classify_error
from services.andrea_sync.goal_runtime import (
    build_goal_continuity_reply,
    ensure_delegate_goal_link,
    try_goal_status_nl_reply,
)
from services.andrea_sync.recovery_engine import recovery_plan_from_message
from services.andrea_sync.resource_router import rank_execution_lanes, routing_explanation
from services.andrea_sync.schema import CommandType, EventType
from services.andrea_sync.turn_intelligence import build_turn_plan, classify_continuity_focus
from services.andrea_sync.store import (
    append_event,
    connect,
    create_task,
    get_task_channel,
    link_task_principal,
    migrate,
)
from services.andrea_sync.workflow_engine import define_linear_workflow, mark_step_done


class BlueprintPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "t.db"
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_goal_bus_and_status_nl(self) -> None:
        create_task(self.conn, "tsk_a", "telegram")
        link_task_principal(self.conn, "tsk_a", "pri_test", channel="telegram")
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_GOAL.value,
                "channel": "internal",
                "payload": {"principal_id": "pri_test", "summary": "Ship feature X"},
            },
        )
        self.assertTrue(r.get("ok"), r)
        gid = str(r.get("goal_id"))
        r2 = handle_command(
            self.conn,
            {
                "command_type": CommandType.LINK_TASK_TO_GOAL.value,
                "channel": "internal",
                "task_id": "tsk_a",
                "payload": {"task_id": "tsk_a", "goal_id": gid},
            },
        )
        self.assertTrue(r2.get("ok"), r2)
        append_event(
            self.conn,
            "tsk_a",
            EventType.JOB_COMPLETED,
            {"summary": "Implemented draft API"},
        )
        reply = try_goal_status_nl_reply(self.conn, "tsk_a", "what's the status?")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn(gid, reply)
        self.assertIn("Ship feature X", reply)
        self.assertIn("completed", reply.lower())

    def test_goal_continuity_reply_without_status_keyword(self) -> None:
        create_task(self.conn, "tsk_a2", "telegram")
        link_task_principal(self.conn, "tsk_a2", "pri_test2", channel="telegram")
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_GOAL.value,
                "channel": "internal",
                "payload": {"principal_id": "pri_test2", "summary": "Prepare weekly report"},
            },
        )
        self.assertTrue(r.get("ok"), r)
        gid = str(r.get("goal_id"))
        linked = handle_command(
            self.conn,
            {
                "command_type": CommandType.LINK_TASK_TO_GOAL.value,
                "channel": "internal",
                "task_id": "tsk_a2",
                "payload": {"task_id": "tsk_a2", "goal_id": gid},
            },
        )
        self.assertTrue(linked.get("ok"), linked)
        reply = build_goal_continuity_reply(self.conn, "tsk_a2")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn(gid, reply)
        self.assertIn("Prepare weekly report", reply)

    def test_auto_goal_link(self) -> None:
        create_task(self.conn, "tsk_b", "cli")
        link_task_principal(self.conn, "tsk_b", "pri_z", channel="cli")
        gid = ensure_delegate_goal_link(
            self.conn,
            "tsk_b",
            user_summary="Do the thing",
            channel="cli",
            auto_create=True,
        )
        self.assertIsNotNone(gid)
        gid2 = ensure_delegate_goal_link(
            self.conn,
            "tsk_b",
            user_summary="ignored",
            channel="cli",
            auto_create=True,
        )
        self.assertEqual(gid, gid2)

    def test_resource_router_and_recovery(self) -> None:
        ranks = rank_execution_lanes(
            "@cursor refactor the typescript service",
            chosen_lane="cursor",
        )
        self.assertEqual(ranks[0][0], "cursor")
        expl = routing_explanation("hello", chosen_lane="direct", routing_hint="andrea")
        self.assertIn("lanes", expl)
        self.assertEqual(classify_error("HTTP 401 unauthorized"), "auth")
        plan = recovery_plan_from_message("timeout connecting to host")
        self.assertEqual(plan["category"], "transport_timeout")

    def test_workflow_and_dashboard(self) -> None:
        wid = define_linear_workflow(self.conn, "pri_w", "nightly", ["a", "b"])
        self.assertTrue(wid.startswith("wfl_"))
        self.assertTrue(mark_step_done(self.conn, wid, "a"))
        summ = _build_blueprint_platform_summary(self.conn)
        self.assertTrue(summ.get("ok"))
        self.assertGreaterEqual(int(summ.get("open_workflows") or 0), 1)
        health = blueprint_platform_health(self.conn)
        self.assertTrue(health.get("ok"))

    def test_get_task_channel_used_by_goal_runtime(self) -> None:
        create_task(self.conn, "tsk_c", "telegram")
        self.assertEqual(get_task_channel(self.conn, "tsk_c"), "telegram")

    def test_turn_plan_domains_cover_core_queries(self) -> None:
        plan_news = build_turn_plan(
            "What's the news today?",
            scenario_id="researchSummary",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_news.domain, "external_information")
        self.assertEqual(plan_news.context_boundary, "external_world_only")
        self.assertFalse(plan_news.allow_goal_continuity_repair)
        self.assertFalse(plan_news.inject_durable_memory)

        plan_news_status_branch = build_turn_plan(
            "What's the news today?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_news_status_branch.domain, "external_information")
        self.assertFalse(plan_news_status_branch.allow_goal_continuity_repair)

        plan_agenda = build_turn_plan(
            "What's on the agenda today?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_agenda.domain, "personal_agenda")
        self.assertFalse(plan_agenda.prefer_state_reply)
        self.assertFalse(plan_agenda.allow_goal_continuity_repair)

        plan_planned_today = build_turn_plan(
            "What's planned today?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_planned_today.domain, "personal_agenda")
        self.assertEqual(plan_planned_today.context_boundary, "personal_agenda_state")

        plan_attention = build_turn_plan(
            "What do I need to pay attention to today?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_attention.domain, "attention_today")
        self.assertEqual(plan_attention.context_boundary, "attention_and_triage_state")
        self.assertFalse(plan_attention.allow_goal_continuity_repair)

        plan_opinion = build_turn_plan(
            "What do you think about that?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_opinion.domain, "opinion_reflection")
        self.assertFalse(plan_opinion.allow_goal_continuity_repair)

        plan_status = build_turn_plan(
            "What are we working on right now?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_status.domain, "project_status")
        self.assertTrue(plan_status.prefer_state_reply)
        self.assertTrue(plan_status.allow_goal_continuity_repair)
        self.assertTrue(plan_status.inject_durable_memory)

        plan_approval = build_turn_plan(
            "What still needs my approval?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_approval.domain, "approval_state")

        plan_exec = build_turn_plan(
            "Please inspect the repo and fix failing tests",
            scenario_id="repoHelpVerified",
            projection_has_continuity_state=False,
        )
        self.assertEqual(plan_exec.domain, "technical_execution")
        self.assertTrue(plan_exec.force_delegate)

        plan_blocked = build_turn_plan(
            "What's blocked right now?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_blocked.domain, "project_status")
        self.assertTrue(plan_blocked.prefer_state_reply)
        self.assertEqual(plan_blocked.continuity_focus, "blocked_state")
        self.assertEqual(classify_continuity_focus("What's blocked right now?"), "blocked_state")

        plan_task_history = build_turn_plan(
            "What happened with that task earlier?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_task_history.domain, "project_status")
        self.assertEqual(plan_task_history.continuity_focus, "recent_outcome_history")

        plan_there = build_turn_plan(
            "What happened there?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_there.continuity_focus, "recent_outcome_history")
        self.assertEqual(classify_continuity_focus("What happened there?"), "recent_outcome_history")

        plan_cursor_task = build_turn_plan(
            "Continue that cursor task",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan_cursor_task.continuity_focus, "cursor_followup_heavy_lift")

        plan_mixed_delegation = build_turn_plan(
            "What did it do?",
            scenario_id="mixedResourceGoal",
            projection_has_continuity_state=False,
            same_chat_delegation_score=60,
        )
        self.assertEqual(plan_mixed_delegation.domain, "project_status")
        self.assertTrue(plan_mixed_delegation.prefer_state_reply)
        self.assertEqual(plan_mixed_delegation.continuity_focus, "recent_outcome_history")

    @mock.patch("services.andrea_sync.goal_runtime.project_task_dict")
    def test_goal_continuity_surfaces_outcome_and_cursor_delegation(self, m_proj: mock.MagicMock) -> None:
        create_task(self.conn, "tsk_out", "telegram")
        link_task_principal(self.conn, "tsk_out", "pri_o", channel="telegram")
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_GOAL.value,
                "channel": "internal",
                "payload": {"principal_id": "pri_o", "summary": "Ship rollout"},
            },
        )
        self.assertTrue(r.get("ok"), r)
        gid = str(r.get("goal_id"))
        linked = handle_command(
            self.conn,
            {
                "command_type": CommandType.LINK_TASK_TO_GOAL.value,
                "channel": "internal",
                "task_id": "tsk_out",
                "payload": {"task_id": "tsk_out", "goal_id": gid},
            },
        )
        self.assertTrue(linked.get("ok"), linked)
        m_proj.return_value = {
            "status": "running",
            "meta": {
                "outcome": {
                    "current_phase": "execution",
                    "current_phase_summary": "Implementing feature X",
                    "blocked_reason": "Waiting for your approval on the rollout plan",
                    "result_kind": "in_progress",
                },
                "execution": {"delegated_to_cursor": True},
                "cursor": {"agent_id": "ag_cursor", "terminal_status": ""},
            },
        }
        reply = build_goal_continuity_reply(self.conn, "tsk_out", user_text="What's blocked right now?")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("Implementing feature X", reply)
        self.assertIn("Waiting for your approval", reply)
        self.assertIn("in_progress", reply)
        self.assertIn("Cursor", reply)


if __name__ == "__main__":
    unittest.main()
