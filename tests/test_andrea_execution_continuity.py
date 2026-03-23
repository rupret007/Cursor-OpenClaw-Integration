"""Tests for goal-centered execution continuity (attempts, lifecycle contract, goal sync)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.andrea_sync.bus import handle_command
from services.andrea_sync.delegated_lifecycle import build_delegated_lifecycle_contract
from services.andrea_sync.execution_runtime import continue_cursor_followup_for_task
from services.andrea_sync.goal_runtime import try_goal_status_nl_reply
from services.andrea_sync.schema import (
    CommandType,
    EventType,
    TaskProjection,
    TaskStatus,
    fold_projection,
)
from services.andrea_sync.store import (
    append_event,
    complete_execution_attempt,
    connect,
    create_execution_attempt,
    create_goal,
    create_task,
    get_active_execution_attempt_for_task,
    link_task_principal,
    link_task_to_goal,
    load_events_for_task,
    migrate,
)


class ExecutionContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ec.db"
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_execution_attempt_create_and_complete(self) -> None:
        create_task(self.conn, "tsk_e", "telegram")
        gid = create_goal(self.conn, "pri_e", "Goal e", channel="telegram")
        eid = create_execution_attempt(
            self.conn,
            "tsk_e",
            gid,
            lane="direct_cursor",
            backend="cursor",
            handle_dict={"cursor_agent_id": "ag_test", "handle_kind": "cursor_agent"},
        )
        self.assertTrue(eid.startswith("atm_"))
        row = get_active_execution_attempt_for_task(self.conn, "tsk_e")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "active")
        handles = json.loads(row["handle_json"])
        self.assertEqual(handles.get("cursor_agent_id"), "ag_test")
        complete_execution_attempt(self.conn, eid, "completed", {"summary": "done"})
        row2 = get_active_execution_attempt_for_task(self.conn, "tsk_e")
        self.assertIsNone(row2)

    def test_goal_status_change_mirrors_to_linked_tasks_via_bus(self) -> None:
        create_task(self.conn, "tsk_g", "telegram")
        link_task_principal(self.conn, "tsk_g", "pri_g", channel="telegram")
        gid = create_goal(self.conn, "pri_g", "Multi-task goal", channel="telegram")
        link_task_to_goal(self.conn, "tsk_g", gid)
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.UPDATE_GOAL_STATUS.value,
                "channel": "internal",
                "payload": {"goal_id": gid, "status": "completed"},
            },
        )
        self.assertTrue(r.get("ok"), r)
        events = load_events_for_task(self.conn, "tsk_g")
        types = [e[2] for e in events]
        self.assertIn(EventType.GOAL_STATUS_CHANGED.value, types)
        payloads = [e[3] for e in events if e[2] == EventType.GOAL_STATUS_CHANGED.value]
        self.assertTrue(payloads)
        self.assertEqual(payloads[-1].get("status"), "completed")
        self.assertEqual(payloads[-1].get("goal_id"), gid)

    def test_delegated_lifecycle_prefers_cursor_agent_id(self) -> None:
        contract = build_delegated_lifecycle_contract(
            {
                "cursor": {
                    "cursor_agent_id": "real_id",
                    "agent_id": "legacy_wrong",
                    "terminal_status": "",
                },
                "execution": {"lane": "openclaw_hybrid", "attempt_id": "atm_x"},
            }
        )
        self.assertEqual(contract["cursor"]["agent_id"], "real_id")
        self.assertEqual(contract["execution"]["attempt_id"], "atm_x")

    def test_fold_goal_status_changed_updates_projection(self) -> None:
        p = TaskProjection(task_id="t", channel="telegram", status=TaskStatus.RUNNING)
        fold_projection(
            p,
            EventType.GOAL_STATUS_CHANGED,
            {"goal_id": "g1", "status": "paused", "summary": "waiting"},
        )
        gm = p.meta.get("goal")
        self.assertIsInstance(gm, dict)
        assert isinstance(gm, dict)
        self.assertEqual(gm.get("goal_id"), "g1")
        self.assertEqual(gm.get("status"), "paused")

    def test_try_goal_status_prefers_task_linked_goal(self) -> None:
        create_task(self.conn, "tsk_cur", "telegram")
        create_task(self.conn, "tsk_other", "telegram")
        link_task_principal(self.conn, "tsk_cur", "pri_m", channel="telegram")
        link_task_principal(self.conn, "tsk_other", "pri_m", channel="telegram")
        g_focus = create_goal(self.conn, "pri_m", "Focused goal", channel="telegram")
        g_noise = create_goal(self.conn, "pri_m", "Other active", channel="telegram")
        link_task_to_goal(self.conn, "tsk_cur", g_focus)
        append_event(
            self.conn,
            "tsk_cur",
            EventType.JOB_STARTED,
            {
                "backend": "cursor",
                "runner": "cursor",
                "cursor_agent_id": "ag_cur",
                "execution_lane": "direct_cursor",
            },
        )
        reply = try_goal_status_nl_reply(self.conn, "tsk_cur", "status?")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn(g_focus, reply)
        self.assertIn("Focused goal", reply)
        self.assertNotIn("Other active", reply)

    @mock.patch(
        "services.andrea_sync.execution_runtime.submit_agent_followup_payload",
        return_value={"ok": True, "returncode": 0, "outer": {}, "response": {}},
    )
    def test_continue_followup_requires_attempt(self, _m: mock.MagicMock) -> None:
        create_task(self.conn, "tsk_f", "telegram")
        out = continue_cursor_followup_for_task(self.conn, "tsk_f", "hello")
        self.assertFalse(out.get("ok"))
        create_execution_attempt(
            self.conn,
            "tsk_f",
            "",
            lane="direct_cursor",
            backend="cursor",
            handle_dict={"cursor_agent_id": "ag_f"},
        )
        out2 = continue_cursor_followup_for_task(self.conn, "tsk_f", "hello")
        self.assertTrue(out2.get("ok"))
        ev = load_events_for_task(self.conn, "tsk_f")
        self.assertTrue(any(e[2] == EventType.JOB_PROGRESS.value for e in ev))


if __name__ == "__main__":
    unittest.main()
