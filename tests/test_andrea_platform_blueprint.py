"""Tests for Andrea platform blueprint modules (goals, router, memory, recovery, workflows)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.andrea_sync.bus import handle_command
from services.andrea_sync.dashboard import _build_blueprint_platform_summary
from services.andrea_sync.experience_assurance import blueprint_platform_health
from services.andrea_sync.failure_classifier import classify_error
from services.andrea_sync.goal_runtime import ensure_delegate_goal_link, try_goal_status_nl_reply
from services.andrea_sync.recovery_engine import recovery_plan_from_message
from services.andrea_sync.resource_router import rank_execution_lanes, routing_explanation
from services.andrea_sync.schema import CommandType, EventType
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


if __name__ == "__main__":
    unittest.main()
