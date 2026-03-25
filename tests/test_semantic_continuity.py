"""Tests for semantic continuity patch (anaphoric follow-ups + same-chat delegation)."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType  # noqa: E402
from services.andrea_sync.semantic_continuity import (  # noqa: E402
    resolve_semantic_continuity_patch,
    same_chat_max_delegation_score,
    same_chat_max_source_truth_score,
    user_message_suggests_anaphoric_cursor_continue,
    user_message_suggests_anaphoric_outcome_recall,
)
from services.andrea_sync.store import append_event, connect, insert_user_outcome_receipt, migrate  # noqa: E402


class TestSemanticContinuity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.conn = connect(Path(self._tmp.name))
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_anaphoric_helpers(self) -> None:
        self.assertTrue(user_message_suggests_anaphoric_outcome_recall("What happened there?"))
        self.assertTrue(user_message_suggests_anaphoric_outcome_recall("What did it do?"))
        self.assertTrue(user_message_suggests_anaphoric_cursor_continue("continue that"))
        self.assertTrue(user_message_suggests_anaphoric_cursor_continue("continue it"))
        self.assertFalse(user_message_suggests_anaphoric_cursor_continue("continue that story"))

    def test_patch_upgrades_continue_that_with_delegation(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-a",
                "payload": {
                    "text": "work",
                    "routing_text": "work",
                    "chat_id": 99001,
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
                "user_summary": "Prior Cursor pass shipped the API change.",
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-b",
                "payload": {
                    "text": "continue that",
                    "routing_text": "continue that",
                    "chat_id": 99001,
                    "message_id": 2,
                },
            },
        )
        tid_b = second["task_id"]
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid_b,
            "continue that",
            scenario_id="statusFollowupContinue",
            base_focus="none",
            projection_has_continuity_state=False,
        )
        self.assertEqual(patch.continuity_focus_override, "cursor_followup_heavy_lift")
        self.assertTrue(patch.force_prefer_state_reply)

    def test_patch_bare_continue_via_same_chat_viability_when_semantic_scores_low(self) -> None:
        """
        Composer viability can see durable receipt rows; semantic meta-only scores stay low.
        Bare continue should still patch to Cursor follow-up.
        """
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-viab-a",
                "payload": {
                    "text": "work",
                    "routing_text": "work",
                    "chat_id": 99011,
                    "message_id": 1,
                },
            },
        )
        tid_a = first["task_id"]
        insert_user_outcome_receipt(
            self.conn,
            receipt_id=str(uuid.uuid4()),
            task_id=tid_a,
            receipt_kind="delegated_outcome",
            summary="Concrete detailed outcome from the Cursor pass with real substance.",
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-viab-b",
                "payload": {
                    "text": "continue that",
                    "routing_text": "continue that",
                    "chat_id": 99011,
                    "message_id": 2,
                },
            },
        )
        tid_b = second["task_id"]
        self.assertLess(same_chat_max_source_truth_score(self.conn, tid_b), 85)
        self.assertLess(same_chat_max_delegation_score(self.conn, tid_b), 28)
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid_b,
            "continue that",
            scenario_id="statusFollowupContinue",
            base_focus="none",
            projection_has_continuity_state=False,
        )
        self.assertEqual(patch.continuity_focus_override, "cursor_followup_heavy_lift")
        self.assertTrue(patch.force_prefer_state_reply)

    def test_patch_empty_without_delegation_signal(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-c",
                "payload": {
                    "text": "continue that",
                    "routing_text": "continue that",
                    "chat_id": 99002,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid,
            "continue that",
            scenario_id="statusFollowupContinue",
            base_focus="none",
            projection_has_continuity_state=False,
        )
        self.assertIsNone(patch.continuity_focus_override)
        self.assertFalse(patch.force_prefer_state_reply)

    def test_patch_upgrades_continue_that_for_mixed_resource_goal(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-mrg-a",
                "payload": {
                    "text": "work",
                    "routing_text": "work",
                    "chat_id": 99004,
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
                "user_summary": "Prior Cursor pass shipped the API change.",
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-mrg-b",
                "payload": {
                    "text": "continue that",
                    "routing_text": "continue that",
                    "chat_id": 99004,
                    "message_id": 2,
                },
            },
        )
        tid_b = second["task_id"]
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid_b,
            "continue that",
            scenario_id="mixedResourceGoal",
            base_focus="none",
            projection_has_continuity_state=False,
        )
        self.assertEqual(patch.continuity_focus_override, "cursor_followup_heavy_lift")
        self.assertTrue(patch.force_prefer_state_reply)

    def test_patch_force_prefer_when_history_focus_and_high_delegation(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-pref",
                "payload": {
                    "text": "status ask",
                    "routing_text": "status ask",
                    "chat_id": 99005,
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
                "user_summary": "Rich enough summary for delegation scoring heuristics here.",
            },
        )
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid,
            "What did Cursor say?",
            scenario_id="statusFollowupContinue",
            base_focus="recent_outcome_history",
            projection_has_continuity_state=False,
        )
        self.assertIsNone(patch.continuity_focus_override)
        self.assertTrue(patch.force_prefer_state_reply)

    def test_patch_skips_non_status_scenario(self) -> None:
        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "semcont-d",
                "payload": {
                    "text": "inbox",
                    "routing_text": "inbox",
                    "chat_id": 99003,
                    "message_id": 1,
                },
            },
        )
        tid = r0["task_id"]
        patch = resolve_semantic_continuity_patch(
            self.conn,
            tid,
            "What happened there?",
            scenario_id="recentMessagesOrInboxLookup",
            base_focus="none",
            projection_has_continuity_state=True,
        )
        self.assertIsNone(patch.continuity_focus_override)


if __name__ == "__main__":
    unittest.main()
