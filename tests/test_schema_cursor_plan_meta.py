"""Projection folding for plan-first Cursor metadata."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.schema import (  # noqa: E402
    EventType,
    TaskProjection,
    TaskStatus,
    fold_projection,
)


class SchemaCursorPlanMetaTests(unittest.TestCase):
    def test_job_completed_folds_cursor_plan_fields(self) -> None:
        proj = TaskProjection(
            task_id="t1",
            status=TaskStatus.RUNNING,
            channel="telegram",
            seq_applied=0,
        )
        fold_projection(
            proj,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "cursor",
                "runner": "cursor",
                "cursor_agent_id": "exec-1",
                "cursor_strategy": "plan_first",
                "planner_agent_id": "plan-9",
                "planner_model": "strong",
                "executor_model": "default",
                "plan_summary": "step one then two",
                "planner_branch": "telegram/t-plan",
                "planner_status": "FINISHED",
            },
        )
        cm = proj.meta.get("cursor", {})
        self.assertEqual(cm.get("cursor_strategy"), "plan_first")
        self.assertEqual(cm.get("planner_agent_id"), "plan-9")
        self.assertEqual(cm.get("executor_model"), "default")
        self.assertIn("step one", str(cm.get("plan_summary") or ""))

    def test_collaboration_recorded_folds_meta(self) -> None:
        proj = TaskProjection(
            task_id="t_collab",
            status=TaskStatus.RUNNING,
            channel="cli",
            seq_applied=0,
        )
        fold_projection(
            proj,
            EventType.COLLABORATION_RECORDED,
            {
                "collab_id": "col_test123",
                "trigger": "verify_fail",
                "pattern": "repair",
                "repair_strategy": "switch_lane",
                "repair_rationale": "Need stronger proof path",
                "arbitration_decision": "accept_repair_plan",
                "plan_id": "plan_1",
            },
        )
        collab = proj.meta.get("collaboration", {})
        self.assertEqual(collab.get("last_collab_id"), "col_test123")
        self.assertEqual(collab.get("last_repair_strategy"), "switch_lane")
        plan_meta = proj.meta.get("plan", {})
        self.assertEqual(plan_meta.get("plan_id"), "plan_1")
        exec_meta = proj.meta.get("execution", {})
        self.assertEqual(exec_meta.get("current_role"), "repair_strategist")
        self.assertGreaterEqual(int(exec_meta.get("repair_attempts") or 0), 1)
