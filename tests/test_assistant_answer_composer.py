"""Unit tests for assistant_answer_composer."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
    AnswerCandidate,
    followthrough_corrective_lead,
    followthrough_needs_user_attention,
    gather_repair_candidates,
    pick_repair_winner,
)
from services.andrea_sync.turn_intelligence import build_turn_plan  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType  # noqa: E402
from services.andrea_sync.telegram_format import format_direct_message  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    append_event,
    connect,
    create_goal,
    create_reminder,
    insert_user_outcome_receipt,
    link_task_principal,
    link_task_to_goal,
    migrate,
)
from services.andrea_sync.projector import project_task_dict  # noqa: E402


class TestAssistantAnswerComposer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.conn = connect(Path(self._tmp.name))
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_followthrough_needs_user_attention(self) -> None:
        self.assertTrue(
            followthrough_needs_user_attention(
                {"last_closure_state": "awaiting_user", "last_closure_reason": ""}
            )
        )
        self.assertFalse(followthrough_needs_user_attention({}))

    def test_followthrough_corrective_lead_on_false_completion(self) -> None:
        ft = {
            "last_closure_state": "pending",
            "last_closure_reason": "Waiting on your OK",
        }
        lead = followthrough_corrective_lead(ft, "You are all caught up, nothing pending.")
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertIn("open item", lead.lower())

    def test_pick_repair_prefers_followthrough_goal_bundle(self) -> None:
        cands = [
            AnswerCandidate(source="model", text="All done!", priority=12),
            AnswerCandidate(
                source="followthrough_goal",
                text="Lead\n\nGoal body",
                priority=96,
            ),
        ]
        got = pick_repair_winner(
            cands,
            model_reply="All done!",
            followthrough={"last_closure_state": "pending"},
            stateful_goal_ok=True,
        )
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got[0], "Lead\n\nGoal body")
        self.assertEqual(got[1], "followthrough_goal")

    def test_projection_includes_followthrough_after_closure_event(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-ft-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77001,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.CLOSURE_DECISION_RECORDED,
            {
                "closure_state": "awaiting_user",
                "reason": "Need your confirmation on the rollout plan.",
                "decision_id": "dec1",
            },
        )
        proj = project_task_dict(self.conn, tid, "telegram")
        meta = proj.get("meta") or {}
        ft = meta.get("followthrough") or {}
        self.assertEqual(ft.get("last_closure_state"), "awaiting_user")

    def test_agenda_reply_lists_upcoming_reminders(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_agenda_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-ag-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77002,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_agenda_comp", channel="telegram")
        create_reminder(
            self.conn,
            principal_id="pri_agenda_comp",
            channel="telegram",
            delivery_target="",
            message="Call dentist",
            due_at=1_700_000_000.0,
            status="scheduled",
            source_task_id=tid,
        )
        text = build_agenda_reply_from_state(self.conn, tid)
        self.assertIn("dentist", text.lower())
        self.assertIn("reminder", text.lower())

    def test_gather_repair_includes_state_rich_goal_when_receipt_snippets_exist(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-srg-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77004,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_srg", channel="telegram")
        gid = create_goal(self.conn, "pri_srg", "Polish the ranking slice", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt_srg_1",
            task_id=tid,
            goal_id=gid,
            receipt_kind="status",
            summary="Merged the composer repair branch.",
        )
        plan = build_turn_plan(
            "What are we working on right now?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        cands = gather_repair_candidates(
            self.conn,
            tid,
            classify_text="What are we working on right now?",
            turn_plan=plan,
            model_reply="Just making progress on things.",
            history=[],
            memory_notes=[],
        )
        sources = [c.source for c in cands]
        self.assertIn("state_rich_goal", sources)

    @mock.patch("services.andrea_sync.goal_runtime.project_task_dict")
    def test_gather_repair_goal_candidate_includes_execution_outcome_summary(
        self, m_proj: mock.MagicMock
    ) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-exo-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77007,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_exo", channel="telegram")
        gid = create_goal(self.conn, "pri_exo", "Delegated slice", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        m_proj.return_value = {
            "status": "running",
            "meta": {
                "outcome": {
                    "current_phase_summary": "Waiting on CI proof for the patch",
                    "blocked_reason": "Tests still red on main",
                },
                "execution": {"delegated_to_cursor": True},
            },
        }
        plan = build_turn_plan(
            "What's blocked right now?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        cands = gather_repair_candidates(
            self.conn,
            tid,
            classify_text="What's blocked right now?",
            turn_plan=plan,
            model_reply="Things are moving along.",
            history=[],
            memory_notes=[],
        )
        texts = " ".join(c.text for c in cands)
        self.assertIn("Waiting on CI proof", texts)
        self.assertIn("Tests still red", texts)
        sources = [c.source for c in cands]
        self.assertIn("blocked_state_reply", sources)

    def test_pick_repair_prefers_blocked_state_reply(self) -> None:
        cands = [
            AnswerCandidate(source="model", text="Hey! How is your day?", priority=12),
            AnswerCandidate(
                source="blocked_state_reply",
                text="The main blocker right now is: waiting on CI.",
                priority=99,
            ),
        ]
        got = pick_repair_winner(
            cands,
            model_reply="Hey! How is your day?",
            followthrough={},
            stateful_goal_ok=False,
        )
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got[1], "blocked_state_reply")

    def test_gather_repair_recent_outcome_prefers_receipts_over_no_active_work(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-roh-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77008,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_roh", channel="telegram")
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt_roh_1",
            task_id=tid,
            goal_id="",
            receipt_kind="outcome",
            summary="Shipped the fix to staging.",
        )
        plan = build_turn_plan(
            "What happened with that task?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan.continuity_focus, "recent_outcome_history")
        cands = gather_repair_candidates(
            self.conn,
            tid,
            classify_text="What happened with that task?",
            turn_plan=plan,
            model_reply="I do not see active tracked work right now.",
            history=[],
            memory_notes=[],
        )
        sources = [c.source for c in cands]
        self.assertIn("cursor_continuity_recall", sources)
        winner = pick_repair_winner(
            cands,
            model_reply="I do not see active tracked work right now.",
            followthrough={},
            stateful_goal_ok=True,
        )
        self.assertIsNotNone(winner)
        assert winner is not None
        self.assertEqual(winner[1], "cursor_continuity_recall")
        self.assertIn("staging", winner[0].lower())

    def test_cursor_continuity_recall_prefers_openclaw_user_summary(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-ccr-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77018,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "Refactored the composer path and added regression tests.",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertIn("Refactored", text)
        self.assertIn("Latest from Cursor / OpenClaw", text)
        self.assertNotRegex(text.lower(), r"task status \*\*created\*\*")

    def test_format_direct_message_strips_soft_failure_boilerplate(self) -> None:
        raw = (
            "I could not complete your request successfully, "
            "but I captured the safe failure summary below. "
            "Here is the substantive recap."
        )
        text = format_direct_message(raw)
        self.assertNotIn("could not complete", text.lower())
        self.assertIn("substantive recap", text.lower())

    def test_gather_repair_includes_attention_state_for_attention_domain(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_attention_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-attn-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77005,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        plan = build_turn_plan(
            "What should I focus on today?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        cands = gather_repair_candidates(
            self.conn,
            tid,
            classify_text="What should I focus on today?",
            turn_plan=plan,
            model_reply="Hard to say.",
            history=[],
            memory_notes=[],
        )
        sources = [c.source for c in cands]
        self.assertIn("attention_state", sources)
        att = build_attention_reply_from_state(self.conn, tid)
        self.assertIn("nothing urgent", att.lower())

    def test_pick_repair_prefers_attention_state_over_weak_model(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_attention_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-attn-2",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77006,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_attn_pick", channel="telegram")
        create_reminder(
            self.conn,
            principal_id="pri_attn_pick",
            channel="telegram",
            delivery_target="",
            message="Review deploy checklist",
            due_at=1_700_000_100.0,
            status="scheduled",
            source_task_id=tid,
        )
        body = build_attention_reply_from_state(self.conn, tid)
        cands = [
            AnswerCandidate(source="model", text="Tell me what you need.", priority=12),
            AnswerCandidate(source="attention_state", text=body, priority=88),
        ]
        got = pick_repair_winner(
            cands,
            model_reply="Tell me what you need.",
            followthrough={},
            stateful_goal_ok=True,
        )
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got[1], "attention_state")
        self.assertIn("deploy", got[0].lower())


if __name__ == "__main__":
    unittest.main()
