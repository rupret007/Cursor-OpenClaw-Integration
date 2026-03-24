"""Tests for Trusted Daily Assistant pack (Stage A): receipts, rollout, onboarding defaults."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.assistant_domain_rollout import (  # noqa: E402
    DAILY_ASSISTANT_SCENARIO_IDS,
    DAILY_PACK_MIN_EVENTS,
    TRUSTED_DAILY_ASSISTANT_PACK_ID,
    build_daily_pack_operator_snapshot,
    daily_pack_live_evidence_report,
    daily_pack_optimizer_hints,
    record_domain_pack_decision,
)
from services.andrea_sync.assistant_receipts import try_record_reminder_receipt  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.collaboration_rollout import (  # noqa: E402
    default_scenario_onboarding_state,
    scenario_onboarding_blocks_live_advisory,
)
from services.andrea_sync.schema import EventType  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    append_event,
    connect,
    create_task,
    insert_domain_repair_outcome_row,
    insert_user_outcome_receipt,
    migrate,
)
from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable  # noqa: E402


class TestAssistantDomainRollout(unittest.TestCase):
    def setUp(self) -> None:
        fd, self._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = connect(Path(self._dbpath))
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self._dbpath)

    def test_daily_scenarios_in_pack(self) -> None:
        self.assertIn("goalContinuationAcrossSessions", DAILY_ASSISTANT_SCENARIO_IDS)
        self.assertIn("statusFollowupContinue", DAILY_ASSISTANT_SCENARIO_IDS)

    def test_default_onboarding_live_direct_blocks_advisory(self) -> None:
        for sid in DAILY_ASSISTANT_SCENARIO_IDS:
            self.assertEqual(default_scenario_onboarding_state(sid), "live_direct")
            self.assertTrue(scenario_onboarding_blocks_live_advisory(self.conn, sid))

    def test_receipt_metrics_and_evidence(self) -> None:
        create_task(self.conn, "tsk_testdaily", "telegram")
        try_record_reminder_receipt(
            self.conn,
            task_id="tsk_testdaily",
            reminder_id="rem_1",
            message="test",
            due_at=1.0,
            status="scheduled",
            delivery_channel="telegram",
            delivery_target="123",
            principal_id="p1",
        )
        snap = build_daily_pack_operator_snapshot(self.conn)
        self.assertEqual(snap.get("pack_id"), TRUSTED_DAILY_ASSISTANT_PACK_ID)
        metrics = snap.get("receipt_metrics") or {}
        self.assertGreaterEqual(int(metrics.get("receipt_count") or 0), 1)
        self.assertIn("proving_signals", snap)
        self.assertIn("routed_task_count_7d", snap.get("proving_signals") or {})
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertFalse(ev.get("evidence_ok"))  # under 30 events
        gd = ev.get("evidence_gate_detail") or {}
        self.assertFalse(gd.get("volume_ok"))
        self.assertIn("blocking_signals", gd)

    def _seed_daily_pack_rows(
        self,
        n: int,
        *,
        receipt_tasks: int | None = None,
        closure_state: str = "",
        pass_hint: bool = True,
        bad_pass_hint_count: int = 0,
    ) -> None:
        """Each row: task + ScenarioResolved + user_outcome_receipt for a daily-pack scenario."""
        sid = "noteOrReminderCapture"
        rcap = receipt_tasks if receipt_tasks is not None else n
        for i in range(n):
            tid = f"tsk_daily_seed_{i}"
            create_task(self.conn, tid, "cli")
            append_event(
                self.conn,
                tid,
                EventType.SCENARIO_RESOLVED,
                {"scenario_id": sid},
            )
            if i >= rcap:
                continue
            ph = pass_hint
            if bad_pass_hint_count > 0 and i < bad_pass_hint_count:
                ph = False
            insert_user_outcome_receipt(
                self.conn,
                receipt_id=f"rcpt_seed_{i}",
                task_id=tid,
                scenario_id=sid,
                pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
                receipt_kind="test",
                summary="unit seed",
                pass_hint=ph,
                closure_state=closure_state,
            )

    def test_evidence_ok_true_when_gates_pass(self) -> None:
        self._seed_daily_pack_rows(DAILY_PACK_MIN_EVENTS)
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertTrue(ev.get("evidence_ok"), ev.get("evidence_gate_detail"))
        gd = ev.get("evidence_gate_detail") or {}
        self.assertTrue(gd.get("volume_ok"))
        self.assertTrue(gd.get("coverage_ok"))
        self.assertTrue(gd.get("quality_ok"))
        self.assertTrue(gd.get("failure_budget_ok"))

    def test_evidence_ok_false_when_coverage_insufficient(self) -> None:
        # Volume floor on receipt rows (30) but routed denominator larger so coverage < 0.90
        sid = "noteOrReminderCapture"
        n_routed = 40
        for i in range(n_routed):
            tid = f"tsk_cov_{i}"
            create_task(self.conn, tid, "cli")
            append_event(
                self.conn,
                tid,
                EventType.SCENARIO_RESOLVED,
                {"scenario_id": sid},
            )
        # 30 receipt rows covering only the first 26 tasks (duplicate rows on four tasks)
        for i in range(26):
            tid = f"tsk_cov_{i}"
            insert_user_outcome_receipt(
                self.conn,
                receipt_id=f"rcpt_cov_a_{i}",
                task_id=tid,
                scenario_id=sid,
                pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
                receipt_kind="test",
                summary="cov",
                pass_hint=True,
            )
        for j in range(4):
            tid = f"tsk_cov_{j}"
            insert_user_outcome_receipt(
                self.conn,
                receipt_id=f"rcpt_cov_b_{j}",
                task_id=tid,
                scenario_id=sid,
                pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
                receipt_kind="test",
                summary="cov dup",
                pass_hint=True,
            )
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertFalse(ev.get("evidence_ok"))
        gd = ev.get("evidence_gate_detail") or {}
        self.assertTrue(gd.get("volume_ok"))
        self.assertFalse(gd.get("coverage_ok"))
        self.assertIn("receipt_coverage_below_threshold", gd.get("blocking_signals") or [])

    def test_evidence_ok_false_when_failure_budget_exceeded(self) -> None:
        self._seed_daily_pack_rows(DAILY_PACK_MIN_EVENTS)
        sid = "noteOrReminderCapture"
        for j in range(4):
            insert_domain_repair_outcome_row(
                self.conn,
                repair_outcome_id=f"dr_fail_{j}",
                domain_id="daily_pack",
                scenario_id=sid,
                task_id=f"tsk_daily_seed_{j}",
                repair_family="unit_test",
                result="failed",
            )
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertFalse(ev.get("evidence_ok"))
        gd = ev.get("evidence_gate_detail") or {}
        self.assertFalse(gd.get("failure_budget_ok"))
        self.assertIn("domain_repair_rate_above_failure_budget", gd.get("blocking_signals") or [])

    def test_evidence_ok_false_when_receipt_quality_low(self) -> None:
        self._seed_daily_pack_rows(DAILY_PACK_MIN_EVENTS, bad_pass_hint_count=4)
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertFalse(ev.get("evidence_ok"))
        gd = ev.get("evidence_gate_detail") or {}
        self.assertFalse(gd.get("quality_ok"))
        self.assertIn("receipt_quality_below_threshold", gd.get("blocking_signals") or [])

    def test_optimizer_hints_failure_pressure_category(self) -> None:
        self._seed_daily_pack_rows(DAILY_PACK_MIN_EVENTS)
        for j in range(4):
            insert_domain_repair_outcome_row(
                self.conn,
                repair_outcome_id=f"dr_hint_{j}",
                domain_id="daily_pack",
                scenario_id="noteOrReminderCapture",
                repair_family="unit_test",
                result="failed",
            )
        hints = daily_pack_optimizer_hints(self.conn)
        cats = [h.get("category") for h in hints]
        self.assertIn("daily_assistant_failure_pressure", cats)

    def test_domain_pack_decision_event(self) -> None:
        res = record_domain_pack_decision(
            self.conn,
            pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
            decision="hold",
            actor="tester",
            reason="unit_test",
        )
        self.assertTrue(res.get("ok"))
        from services.andrea_sync.store import load_events_for_task, SYSTEM_TASK_ID

        ensure = load_events_for_task(self.conn, SYSTEM_TASK_ID)
        types = [row[2] for row in ensure]
        self.assertIn(EventType.DOMAIN_ROLLOUT_DECISION_RECORDED.value, types)

    def test_telegram_continuation_records(self) -> None:
        create_task(self.conn, "tsk_anchor", "telegram")
        cmd = {
            "command_type": "SubmitUserMessage",
            "channel": "telegram",
            "payload": {
                "text": "hello",
                "chat_id": 99,
                "from_user": 1,
                "message_id": 50,
            },
        }
        r1 = handle_command(self.conn, cmd)
        self.assertTrue(r1.get("ok"))
        tid = str(r1.get("task_id") or "")
        cmd2 = {
            "command_type": "SubmitUserMessage",
            "channel": "telegram",
            "payload": {
                "text": "follow up chunk",
                "chat_id": 99,
                "from_user": 1,
                "message_id": 51,
            },
        }
        attached = attach_continuation_if_applicable(self.conn, cmd2)
        self.assertTrue(attached)
        self.assertEqual(cmd2.get("task_id"), tid)
        from services.andrea_sync.store import list_recent_continuation_records

        rows = list_recent_continuation_records(self.conn, limit=5)
        self.assertTrue(any(str(r["linked_task_id"] or "") == tid for r in rows))


if __name__ == "__main__":
    unittest.main()
