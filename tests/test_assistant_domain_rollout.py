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
    TRUSTED_DAILY_ASSISTANT_PACK_ID,
    build_daily_pack_operator_snapshot,
    daily_pack_live_evidence_report,
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
    connect,
    create_task,
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
        ev = daily_pack_live_evidence_report(self.conn)
        self.assertFalse(ev.get("evidence_ok"))  # under 30 events

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
