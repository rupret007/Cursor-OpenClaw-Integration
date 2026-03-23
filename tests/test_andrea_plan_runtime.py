"""Tests for plan / verify / recover orchestrator (durable plans + approval + verification)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.andrea_router import route_message  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.plan_runtime import (  # noqa: E402
    finalize_execute_step_verification,
    gate_delegated_job,
)
from services.andrea_sync.scenario_runtime import resolve_scenario  # noqa: E402
from services.andrea_sync.scenario_schema import scenario_blob_for_job_payload  # noqa: E402
from services.andrea_sync.projector import project_task_dict  # noqa: E402
from services.andrea_sync.schema import EventType, TaskStatus, legal_task_transition  # noqa: E402
from services.andrea_sync.scenario_registry import get_contract  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    connect,
    create_task,
    get_active_execution_plan_for_task,
    get_execution_plan,
    get_plan_step,
    link_task_principal,
    migrate,
)
from services.andrea_sync.dashboard import build_dashboard_summary  # noqa: E402


class _FakeServer:
    db_path = ""
    telegram_public_base = ""
    telegram_bot_token = ""
    telegram_header_secret = ""
    telegram_secret = ""
    telegram_use_query_secret = False
    telegram_webhook_autofix = False
    background_enabled = False
    delegated_execution_enabled = False
    background_optimizer_enabled = False
    background_incident_repair_enabled = False
    openclaw_agent_id = ""
    telegram_notifier_enabled = False
    telegram_quiet_lifecycle = False
    telegram_auto_cursor = False
    telegram_delegate_lane = "cursor"


class TestPlanRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._prev = {
            "ANDREA_SYNC_DB": os.environ.get("ANDREA_SYNC_DB"),
            "ANDREA_SYNC_FORCE_DELEGATE_APPROVAL": os.environ.get(
                "ANDREA_SYNC_FORCE_DELEGATE_APPROVAL"
            ),
            "ANDREA_SYNC_STRICT_VERIFICATION": os.environ.get(
                "ANDREA_SYNC_STRICT_VERIFICATION"
            ),
        }
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
        os.environ.pop("ANDREA_SYNC_FORCE_DELEGATE_APPROVAL", None)
        os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = "0"
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_auto_allowed_creates_plan(self) -> None:
        create_task(self.conn, "tsk_plan_a", "cli")
        link_task_principal(self.conn, "tsk_plan_a", "p_test", channel="cli")
        job = {
            "kind": "cursor",
            "runner": "cursor",
            "execution_lane": "cursor",
            "prompt_excerpt": "x",
        }
        gate = gate_delegated_job(
            self.conn,
            "tsk_plan_a",
            "",
            "p_test",
            "fix the repo",
            "cursor",
            job,
            [["cursor", 1.0]],
        )
        self.assertEqual(gate.mode, "proceed")
        self.assertTrue(gate.plan_id)
        plan = get_active_execution_plan_for_task(self.conn, "tsk_plan_a")
        assert plan is not None
        self.assertEqual(plan["task_id"], "tsk_plan_a")
        st = get_plan_step(self.conn, gate.execute_step_id)
        assert st is not None
        self.assertEqual(st["step_kind"], "execute_delegated")

    def test_plan_kind_from_scenario_contract(self) -> None:
        create_task(self.conn, "tsk_plan_kind", "cli")
        link_task_principal(self.conn, "tsk_plan_kind", "p_test", channel="cli")
        text = "fix tests in foo.py"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        job = {
            "kind": "cursor",
            "runner": "cursor",
            "execution_lane": "cursor",
            "scenario": scenario_blob_for_job_payload(r, c),
        }
        gate = gate_delegated_job(
            self.conn, "tsk_plan_kind", "", "p_test", text, "cursor", job, []
        )
        self.assertEqual(gate.mode, "proceed")
        plan = get_active_execution_plan_for_task(self.conn, "tsk_plan_kind")
        assert plan is not None
        self.assertEqual(plan.get("plan_kind"), "delegated_repo_task")

        create_task(self.conn, "tsk_plan_kind_mix", "cli")
        link_task_principal(self.conn, "tsk_plan_kind_mix", "p_test", channel="cli")
        gate2 = gate_delegated_job(
            self.conn,
            "tsk_plan_kind_mix",
            "",
            "p_test",
            "x",
            "cursor",
            {"kind": "cursor", "runner": "cursor"},
            [],
        )
        self.assertEqual(gate2.mode, "proceed")
        plan2 = get_active_execution_plan_for_task(self.conn, "tsk_plan_kind_mix")
        assert plan2 is not None
        self.assertEqual(plan2.get("plan_kind"), "mixed")

    def test_forced_approval_blocks_queue(self) -> None:
        os.environ["ANDREA_SYNC_FORCE_DELEGATE_APPROVAL"] = "1"
        create_task(self.conn, "tsk_plan_b", "cli")
        link_task_principal(self.conn, "tsk_plan_b", "p_test", channel="cli")
        job = {"kind": "cursor", "runner": "cursor", "execution_lane": "cursor"}
        gate = gate_delegated_job(
            self.conn,
            "tsk_plan_b",
            "",
            "p_test",
            "danger",
            "cursor",
            job,
            [],
        )
        self.assertEqual(gate.mode, "await_approval")
        self.assertTrue(gate.approval_id)

    def test_resolve_approval_then_job_queued(self) -> None:
        os.environ["ANDREA_SYNC_FORCE_DELEGATE_APPROVAL"] = "1"
        create_task(self.conn, "tsk_plan_c", "cli")
        link_task_principal(self.conn, "tsk_plan_c", "p_test", channel="cli")
        gate = gate_delegated_job(
            self.conn,
            "tsk_plan_c",
            "",
            "p_test",
            "work",
            "cursor",
            {"kind": "cursor", "runner": "cursor"},
            [],
        )
        aid = gate.approval_id
        body = {
            "command_type": "ResolveGoalApproval",
            "channel": "internal",
            "task_id": "tsk_plan_c",
            "payload": {"approval_id": aid, "resolution": "approved"},
        }
        res = handle_command(self.conn, body)
        self.assertTrue(res.get("ok"), res)
        events = self.conn.execute(
            "SELECT event_type FROM events WHERE task_id = ? ORDER BY seq",
            ("tsk_plan_c",),
        ).fetchall()
        types = [r[0] for r in events]
        self.assertIn(EventType.JOB_QUEUED.value, types)

    def test_verification_fail_without_pr(self) -> None:
        os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = "1"
        create_task(self.conn, "tsk_plan_d", "cli")
        link_task_principal(self.conn, "tsk_plan_d", "p_test", channel="cli")
        gate = gate_delegated_job(
            self.conn,
            "tsk_plan_d",
            "",
            "p_test",
            "x",
            "cursor",
            {"kind": "cursor", "runner": "cursor"},
            [],
        )
        fv = finalize_execute_step_verification(
            self.conn,
            task_id="tsk_plan_d",
            plan_id=gate.plan_id,
            execute_step_id=gate.execute_step_id,
            terminal_status="FINISHED",
            pr_url="",
            agent_url="",
            lane="cursor",
        )
        self.assertEqual(fv.get("verdict"), "fail")
        self.assertFalse(fv.get("should_complete_job"))

    def test_collaboration_metadata_on_verify_fail_repo_help(self) -> None:
        os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = "1"
        os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = "1"
        create_task(self.conn, "tsk_plan_collab", "cli")
        link_task_principal(self.conn, "tsk_plan_collab", "p_test", channel="cli")
        c = get_contract("repoHelpVerified")
        assert c is not None
        scen_blob = {
            "scenario_id": "repoHelpVerified",
            "support_level": c.support_level,
            "action_class": c.action_class,
            "proof_class": c.proof_class,
            "receipt_state": "pending",
            "approval_mode": c.approval_mode,
        }
        gate = gate_delegated_job(
            self.conn,
            "tsk_plan_collab",
            "",
            "p_test",
            "x",
            "cursor",
            {"kind": "cursor", "runner": "cursor", "scenario": scen_blob},
            [],
        )
        fv = finalize_execute_step_verification(
            self.conn,
            task_id="tsk_plan_collab",
            plan_id=gate.plan_id,
            execute_step_id=gate.execute_step_id,
            terminal_status="FINISHED",
            pr_url="",
            agent_url="",
            lane="cursor",
        )
        self.assertIsInstance(fv.get("collaboration_event_payload"), dict)
        self.assertTrue(str(fv["collaboration_event_payload"].get("collab_id") or "").strip())
        row = get_execution_plan(self.conn, gate.plan_id)
        summ = row.get("summary") if isinstance(row, dict) else {}
        self.assertTrue((summ.get("collaboration") or {}).get("rounds"))

    def test_legal_transition_job_queued_from_awaiting_approval(self) -> None:
        ok, st = legal_task_transition(TaskStatus.AWAITING_APPROVAL, EventType.JOB_QUEUED)
        self.assertTrue(ok)
        self.assertEqual(st, TaskStatus.QUEUED)

    def test_dashboard_includes_plan_orchestration(self) -> None:
        fake = _FakeServer()
        fake.db_path = str(self.db_path)
        summ = build_dashboard_summary(self.conn, fake, limit=5)
        self.assertTrue(summ.get("ok"))
        po = summ.get("plan_orchestration") or {}
        self.assertTrue(po.get("ok"))
        self.assertIn("scenario_counts", po)
        self.assertIn("proof_coverage", po)
        self.assertIn("receipt_state_counts", po)
        self.assertIn("first_pack_health", po)


if __name__ == "__main__":
    unittest.main()
