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
    build_execution_outcome_surface_bundle,
    build_stateful_summary_bundle,
    bounded_composer_repair,
    draft_should_force_continuity_repair,
    followthrough_corrective_lead,
    followthrough_needs_user_attention,
    gather_cursor_recall_evidence_pack,
    gather_repair_candidates,
    is_generic_execution_wrapper_text,
    pick_repair_winner,
)
from services.andrea_sync.turn_intelligence import build_turn_plan  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType  # noqa: E402
from services.andrea_sync.telegram_format import format_direct_message  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    append_event,
    connect,
    create_execution_attempt,
    create_goal,
    create_reminder,
    insert_user_outcome_receipt,
    link_task_principal,
    link_task_to_goal,
    migrate,
)
from services.andrea_sync.projector import project_task_dict  # noqa: E402
from services.andrea_sync.scenario_registry import SCENARIO_CATALOG  # noqa: E402
from services.andrea_sync.scenario_schema import ScenarioResolution  # noqa: E402


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

    @mock.patch("services.andrea_sync.assistant_answer_composer.summarize_execution_attempt_for_user")
    def test_agenda_reply_surfaces_top_plan_bundle_with_execution_context(
        self,
        mock_exec: mock.MagicMock,
    ) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_agenda_reply_from_state,
        )

        mock_exec.return_value = {
            "ok": True,
            "lane": "openclaw_collaboration_state_answer",
            "status": "blocked",
        }
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-ag-priority-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77002,
                    "message_id": 2,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_agenda_priority_comp", channel="telegram")
        create_reminder(
            self.conn,
            principal_id="pri_agenda_priority_comp",
            channel="telegram",
            delivery_target="",
            message="Review deploy checklist",
            due_at=1_700_000_200.0,
            status="scheduled",
            source_task_id=tid,
        )
        text = build_agenda_reply_from_state(self.conn, tid)
        low = text.lower()
        self.assertIn("top plan items i can see right now", low)
        self.assertIn("review deploy checklist", low)
        self.assertIn("execution context", low)
        self.assertIn("full calendar view", low)

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

    def test_recall_pack_uses_receipt_reply_excerpt_when_summary_placeholder(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-rcpt-excerpt-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77013,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt_excerpt_1",
            task_id=tid,
            receipt_kind="status",
            summary="Status / follow-up reply (goal_runtime_status).",
            proof_refs={"reply_excerpt": "Cursor fixed retry policy and reran smoke tests cleanly."},
        )
        pack = gather_cursor_recall_evidence_pack(
            self.conn,
            tid,
            user_message="Where are we on the project overall?",
        )
        lines = " ".join(pack.source_truth_receipt_lines)
        self.assertIn("receipt excerpt", lines.lower())
        self.assertIn("retry policy", lines.lower())

    def test_blocked_bundle_surfaces_primary_blocker_detail(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-blocked-bundle-1",
                "payload": {
                    "text": "status",
                    "routing_text": "status",
                    "chat_id": 77014,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        with mock.patch(
            "services.andrea_sync.assistant_answer_composer._projection_meta"
        ) as mock_meta:
            mock_meta.return_value = {
                "outcome": {
                    "blocked_reason": "Staging credentials are missing for deploy validation.",
                    "current_phase_summary": "Waiting on deploy verification",
                }
            }
            bundle = build_stateful_summary_bundle(
                self.conn,
                tid,
                source="blocked_state_reply",
                user_message="What is OpenClaw blocked on?",
                deterministic_reply="The main blocker right now is: waiting.",
            )
        self.assertIn("staging credentials", bundle.primary_finding.lower())
        self.assertGreaterEqual(bundle.evidence_strength, 3)

    def test_pick_repair_blocks_openclaw_blocker_on_identity_turn(self) -> None:
        cands = [
            AnswerCandidate(source="model", text="Sure.", priority=12),
            AnswerCandidate(
                source="blocked_state_reply",
                text=(
                    "The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
                ),
                priority=99,
            ),
        ]
        turn_plan = build_turn_plan(
            "Is this OpenClaw?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        got = pick_repair_winner(
            cands,
            model_reply="Sure.",
            followthrough={},
            stateful_goal_ok=True,
            classify_text="Is this OpenClaw?",
            turn_plan=turn_plan,
        )
        self.assertIsNone(got)

    def test_pick_repair_allows_openclaw_blocker_when_explicitly_asked(self) -> None:
        cands = [
            AnswerCandidate(source="model", text="I can check that.", priority=12),
            AnswerCandidate(
                source="blocked_state_reply",
                text=(
                    "The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
                ),
                priority=99,
            ),
        ]
        turn_plan = build_turn_plan(
            "Why was OpenClaw blocked?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        got = pick_repair_winner(
            cands,
            model_reply="I can check that.",
            followthrough={},
            stateful_goal_ok=True,
            classify_text="Why was OpenClaw blocked?",
            turn_plan=turn_plan,
        )
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got[1], "blocked_state_reply")

    def test_stateful_next_steps_are_error_specific_for_cursor_recall(self) -> None:
        from services.andrea_sync.assistant_answer_composer import derive_stateful_next_step_options

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-next-opt-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77031,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        opts = derive_stateful_next_step_options(
            self.conn,
            tid,
            source="cursor_continuity_recall",
            user_text="What did Cursor say about this traceback error?",
        )
        self.assertTrue(any(("traceback" in o.lower() or "error" in o.lower()) for o in opts))

    def test_stateful_next_steps_for_goal_status_offer_action_shape(self) -> None:
        from services.andrea_sync.assistant_answer_composer import derive_stateful_next_step_options

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-next-opt-2",
                "payload": {
                    "text": "status",
                    "routing_text": "status",
                    "chat_id": 77032,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        opts = derive_stateful_next_step_options(
            self.conn,
            tid,
            source="goal_status",
            user_text="Status update please",
        )
        self.assertTrue(any("recap" in o.lower() and "continue action" in o.lower() for o in opts))

    def test_draft_should_force_continuity_repair_detects_metadata_scaffold(self) -> None:
        thin = (
            "Where things stand: task status **created**; result: **queued**; "
            "phase: **pending**; result kind **none**."
        )
        self.assertTrue(
            draft_should_force_continuity_repair(thin, "What did Cursor say?")
        )
        self.assertFalse(draft_should_force_continuity_repair("", "hi"))

    def test_bounded_repair_replaces_metadata_heavy_model_draft(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-mech-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77088,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_mech", channel="telegram")
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt_mech_1",
            task_id=tid,
            goal_id="",
            receipt_kind="outcome",
            summary="Merged the hotfix and tagged v1.2.3.",
        )
        plan = build_turn_plan(
            "What happened there?",
            scenario_id="statusFollowupContinue",
            projection_has_continuity_state=True,
        )
        self.assertEqual(plan.continuity_focus, "recent_outcome_history")
        sc = SCENARIO_CATALOG["statusFollowupContinue"]
        resolution = ScenarioResolution(
            scenario_id=sc.scenario_id,
            confidence=0.9,
            support_level=sc.support_level,
            reason="test",
            goal_id="",
            needs_plan=False,
            suggested_lane="direct_assistant",
            action_class=sc.action_class,
            proof_class=sc.proof_class,
            approval_mode=sc.approval_mode,
        )
        mechanical = (
            "Where things stand: task status **created**; result: **queued**; "
            "phase: **pending**; result kind **none**."
        )
        repaired = bounded_composer_repair(
            self.conn,
            tid,
            classify_text="What happened there?",
            decision_reply=mechanical,
            decision_reason="balanced_default_direct",
            resolution=resolution,
            turn_plan=plan,
            history=[],
            memory_notes=[],
            continuity_ask=False,
            continuity_state=False,
        )
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertIn("v1.2.3", repaired[0])

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
        self.assertIn("Cursor recap:", text)
        self.assertNotRegex(text.lower(), r"task status \*\*created\*\*")

    def test_cursor_recall_low_info_path_does_not_read_unbound_has_narrative(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-unbound-has-narrative",
                "payload": {
                    "text": "seed",
                    "routing_text": "seed",
                    "chat_id": 88141,
                    "message_id": 1,
                },
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "LOW_INFO_RECALL_MARKER_41 delegated recap marker.",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertIn("LOW_INFO_RECALL_MARKER_41", text)
        self.assertIn("Cursor recap:", text)

    def test_cursor_recall_lead_prefers_source_truth_over_long_assistant_surface(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-src-beat-derived",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 77019,
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
                "user_summary": "SHORT_SOURCE_TRUTH_PREFERRED_LEAD.",
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "LONG_ASSISTANT_BOILERPLATE " * 20 + "more filler here.",
                "route": "direct",
                "reason": "test",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertIn("SHORT_SOURCE_TRUTH_PREFERRED_LEAD", text)
        self.assertIn("Cursor recap:", text)
        lead_line = text.split("Cursor recap:", 1)[1].split("\n")[0]
        self.assertIn("SHORT_SOURCE_TRUTH", lead_line)
        self.assertNotIn("LONG_ASSISTANT_BOILERPLATE", lead_line)

    def test_cursor_recall_ranks_richer_same_chat_task_over_thin_current(self) -> None:
        """Older same-chat task with OpenClaw narrative beats a newer thin shell task."""
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-rank-a",
                "payload": {
                    "text": "first",
                    "routing_text": "first",
                    "chat_id": 88050,
                    "message_id": 1,
                },
            },
        )
        tid_a = first["task_id"]
        append_event(
            self.conn,
            tid_a,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "DELEGATED_RICH_SUMMARY_UNIQUE_XYZ older workstream result.",
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-rank-b",
                "payload": {
                    "text": "second",
                    "routing_text": "second",
                    "chat_id": 88050,
                    "message_id": 2,
                },
            },
        )
        tid_b = second["task_id"]
        self.assertNotEqual(tid_a, tid_b)
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid_b, user_message="What did Cursor say?"
        )
        self.assertIn("DELEGATED_RICH_SUMMARY_UNIQUE_XYZ", text)

    def test_cursor_recall_rejects_approval_inventory_and_status_followup_receipt(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-approval-sludge-1",
                "payload": {
                    "text": "check approvals",
                    "routing_text": "check approvals",
                    "chat_id": 88063,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "I'm not seeing any approval requests waiting on you right now.",
                "route": "direct",
                "reason": "goal_runtime_status",
            },
        )
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt-approval-sludge-1",
            task_id=tid,
            receipt_kind="status_followup",
            summary="Status / follow-up reply (goal_runtime_status).",
            proof_refs={"reason": "goal_runtime_status"},
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        low = text.lower()
        self.assertIn("not finding a recent clean cursor result", low)
        self.assertNotIn("approval requests waiting", low)
        self.assertNotIn("status / follow-up reply", low)
        self.assertNotIn("goal_runtime_status", low)

    def test_cursor_recall_same_chat_prefers_domain_neighbor_over_approval_sludge(self) -> None:
        """Same-chat task with hard Cursor markers wins recall over a newer sludge-only task."""
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-domain-gate-a",
                "payload": {
                    "text": "delegated work a",
                    "routing_text": "delegated work a",
                    "chat_id": 88064,
                    "message_id": 1,
                },
            },
        )
        tid_a = first["task_id"]
        append_event(
            self.conn,
            tid_a,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "CURSOR_NEIGHBOR_DOMAIN_GATE_99 real delegated recap anchor.",
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-domain-gate-b",
                "payload": {
                    "text": "approval noise b",
                    "routing_text": "approval noise b",
                    "chat_id": 88064,
                    "message_id": 2,
                },
            },
        )
        tid_b = second["task_id"]
        append_event(
            self.conn,
            tid_b,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "I'm not seeing any approval requests waiting on you right now.",
                "route": "direct",
            },
        )
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt-domain-gate-b",
            task_id=tid_b,
            receipt_kind="status_followup",
            summary="Status / follow-up reply (goal_runtime_status).",
            proof_refs={"reason": "goal_runtime_status"},
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid_b, user_message="What happened in the Cursor thread?"
        )
        self.assertIn("CURSOR_NEIGHBOR_DOMAIN_GATE_99", text)
        self.assertNotIn("approval requests waiting", text.lower())

    def test_cursor_recall_includes_active_execution_attempt_line(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-exec-recall-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 88052,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_exec_recall", channel="telegram")
        gid = create_goal(self.conn, "pri_exec_recall", "Track recall", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        create_execution_attempt(
            self.conn,
            tid,
            gid,
            lane="direct_cursor",
            backend="cursor",
            handle_dict={"cursor_agent_id": "ag_exec_recall", "handle_kind": "cursor_agent"},
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor do?"
        )
        self.assertIn("Delegated execution (tracked)", text)
        self.assertIn("direct_cursor", text)

    def test_cursor_recall_demotes_execution_scaffold_when_narrative_exists(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-recap-priority-1",
                "payload": {
                    "text": "hi",
                    "routing_text": "hi",
                    "chat_id": 88059,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_recap_priority", channel="telegram")
        gid = create_goal(self.conn, "pri_recap_priority", "Track recap", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "Delivered the root-cause recap and implementation plan.",
            },
        )
        create_execution_attempt(
            self.conn,
            tid,
            gid,
            lane="direct_cursor",
            backend="cursor",
            handle_dict={"cursor_agent_id": "ag_recap_priority", "handle_kind": "cursor_agent"},
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        first = text.splitlines()[0] if text else ""
        self.assertIn("Cursor recap:", first)
        self.assertNotIn("Delegated execution (tracked)", first)

    def test_cursor_recall_skips_echo_projection_summary(self) -> None:
        """Do not surface the user's question as a 'Recorded summary' when it matches."""
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-echo-1",
                "payload": {
                    "text": "What did Cursor say?",
                    "routing_text": "What did Cursor say?",
                    "chat_id": 88051,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertNotIn("Recorded summary: What did Cursor say?", text)
        self.assertIn("not finding a recent clean cursor result", text.lower())

    def test_cursor_recall_without_domain_markers_falls_back_cleanly(self) -> None:
        """Assistant-only updates do not satisfy explicit Cursor recall without delegated markers."""
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-phase-summary-1",
                "payload": {
                    "text": "Status please",
                    "routing_text": "Status please",
                    "chat_id": 88061,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "Drafted the recap strategy and queued targeted tests.",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertIn("not finding a recent clean cursor result", text.lower())
        self.assertNotIn("Drafted the recap strategy", text)

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

    def test_cursor_recall_respects_message_thread_boundary(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-thread-a",
                "payload": {
                    "text": "thread a",
                    "routing_text": "thread a",
                    "chat_id": 88110,
                    "message_id": 1,
                    "message_thread_id": 701,
                },
            },
        )
        tid_a = first["task_id"]
        append_event(
            self.conn,
            tid_a,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "THREAD_ALPHA_MARKER richer summary",
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-thread-b",
                "payload": {
                    "text": "thread b",
                    "routing_text": "thread b",
                    "chat_id": 88110,
                    "message_id": 2,
                    "message_thread_id": 702,
                },
            },
        )
        tid_b = second["task_id"]
        append_event(
            self.conn,
            tid_b,
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "THREAD_BETA_MARKER summary",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid_b, user_message="What did Cursor say?"
        )
        self.assertIn("THREAD_BETA_MARKER", text)
        self.assertNotIn("THREAD_ALPHA_MARKER", text)

    def test_cursor_recall_strips_recursive_recap_prefix(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-recap-recursive",
                "payload": {
                    "text": "seed",
                    "routing_text": "seed",
                    "chat_id": 88111,
                    "message_id": 1,
                },
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "Cursor recap: Cursor recap: fixed the bug and queued verification.",
                "route": "direct",
                "reason": "test",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertNotIn("Cursor recap: Cursor recap:", text)

    def test_cursor_recall_prefers_openclaw_over_derived_assistant_recap(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            build_cursor_continuity_recall_reply_from_state,
        )

        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-src-truth-derived",
                "payload": {
                    "text": "seed",
                    "routing_text": "seed",
                    "chat_id": 88133,
                    "message_id": 1,
                },
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "Cursor recap: DERIVED_NOISE_MARK_999 should not win the lead.",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "OPENCLAW_LEAD_MARK_123 primary source-truth narrative.",
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "Cursor recap: DERIVED_NOISE_MARK_999 should not win the lead.",
                "route": "direct",
                "reason": "test",
            },
        )
        text = build_cursor_continuity_recall_reply_from_state(
            self.conn, tid, user_message="What did Cursor say?"
        )
        self.assertIn("OPENCLAW_LEAD_MARK_123", text)
        self.assertNotIn("DERIVED_NOISE_MARK_999", text)

    def test_find_viable_recent_cursor_workstream_uses_rich_neighbor(self) -> None:
        from services.andrea_sync.assistant_answer_composer import (  # noqa: E402
            find_viable_recent_cursor_workstream_reply,
        )

        rich = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-viable-rich",
                "payload": {
                    "text": "first",
                    "routing_text": "first",
                    "chat_id": 88134,
                    "message_id": 1,
                },
            },
        )
        append_event(
            self.conn,
            rich["task_id"],
            EventType.JOB_COMPLETED,
            {
                "summary": "done",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "VIABLE_NEIGHBOR_RECAP_MARK_77 authoritative recap.",
            },
        )
        thin = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-viable-thin",
                "payload": {
                    "text": "hey",
                    "routing_text": "hey",
                    "chat_id": 88134,
                    "message_id": 2,
                },
            },
        )
        out = find_viable_recent_cursor_workstream_reply(
            self.conn, thin["task_id"], user_message="What are we doing?"
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("VIABLE_NEIGHBOR_RECAP_MARK_77", out)

    def test_is_generic_execution_wrapper_text_distinguishes_substantive_outcome(self) -> None:
        self.assertTrue(is_generic_execution_wrapper_text("OpenClaw completed the delegated task."))
        self.assertTrue(is_generic_execution_wrapper_text("I finished your request."))
        self.assertFalse(
            is_generic_execution_wrapper_text(
                "Cursor completed rollout and smoke checks cleanly with no regressions."
            )
        )

    def test_execution_outcome_surface_bundle_prefers_phase_and_receipt_substance(self) -> None:
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "comp-outcome-bundle",
                "payload": {
                    "text": "please continue",
                    "routing_text": "please continue",
                    "chat_id": 88135,
                    "message_id": 1,
                },
            },
        )
        tid = result["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "OpenClaw completed the delegated task.",
                "backend": "openclaw",
                "runner": "openclaw",
                "user_summary": "OpenClaw completed the delegated task.",
                "phase_outputs": {
                    "execution": {"summary": "Applied the retry patch and reran the failing tests."},
                    "synthesis": {"summary": "All tests passed and the fix is ready to ship."},
                },
            },
        )
        insert_user_outcome_receipt(
            self.conn,
            receipt_id="rcpt_outcome_bundle_1",
            task_id=tid,
            scenario_id="statusFollowupContinue",
            pack_id="trusted_daily_assistant",
            receipt_kind="outcome",
            summary="Assistant outcome (goal_runtime_status).",
            proof_refs={"reply_excerpt": "Receipt says: fixed retries and stabilized the workflow."},
        )
        bundle = build_execution_outcome_surface_bundle(self.conn, tid, user_message="what happened?")
        self.assertIn("tests passed", bundle.primary_finding.lower())
        joined = " ".join(bundle.evidence_lines).lower()
        self.assertIn("retry patch", joined)
        self.assertIn("receipt says", joined)


if __name__ == "__main__":
    unittest.main()
