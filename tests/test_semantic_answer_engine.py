from __future__ import annotations

import unittest
from unittest import mock

from services.andrea_sync.semantic_answer_engine import choose_semantic_state_reply
from services.andrea_sync.turn_intelligence import TurnPlan


class SemanticAnswerEngineTests(unittest.TestCase):
    def _turn_plan(self, *, focus: str) -> TurnPlan:
        return TurnPlan(
            domain="project_status",
            context_boundary="project_continuity_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=False,
            continuity_focus=focus,
        )

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    @mock.patch(
        "services.andrea_sync.semantic_answer_engine.build_recent_outcome_history_reply_from_state"
    )
    @mock.patch("services.andrea_sync.semantic_answer_engine.maybe_realize_stateful_reply")
    def test_explicit_cursor_recall_does_not_lose_to_goal_continuity(
        self,
        mock_realize: mock.MagicMock,
        mock_recent: mock.MagicMock,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_realize.return_value = None
        mock_recent.return_value = (
            "Cursor recap: Added retries and fixed timeout handling."
        )
        mock_goal_status.return_value = None
        mock_goal_cont.return_value = (
            "Goal `g1` is still running with a long status narrative that used to outrank recap."
        )

        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t1",
            user_text="What did Cursor say?",
            turn_plan=self._turn_plan(focus="recent_outcome_history"),
            scenario_id="statusFollowupContinue",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "cursor_continuity_recall")
        self.assertIn("Cursor recap:", result.reply_text)
        self.assertGreaterEqual(result.score, 70)

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    @mock.patch(
        "services.andrea_sync.semantic_answer_engine.build_recent_outcome_history_reply_from_state"
    )
    @mock.patch("services.andrea_sync.semantic_answer_engine.maybe_realize_stateful_reply")
    def test_stateful_realization_can_replace_surface_text(
        self,
        mock_realize: mock.MagicMock,
        mock_recent: mock.MagicMock,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_recent.return_value = "Cursor recap: Added retries and fixed timeout handling."
        mock_goal_status.return_value = None
        mock_goal_cont.return_value = None
        mock_realize.return_value = "Cursor finished retries and tightened timeout handling."

        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-realized",
            user_text="What did Cursor say?",
            turn_plan=self._turn_plan(focus="recent_outcome_history"),
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("tightened timeout", result.reply_text)

    def test_returns_none_for_non_stateful_domain(self) -> None:
        turn_plan = TurnPlan(
            domain="external_information",
            context_boundary="external_world_only",
            prefer_state_reply=False,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=False,
            inject_durable_memory=False,
            continuity_focus="none",
        )
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t2",
            user_text="What happened there?",
            turn_plan=turn_plan,
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNone(result)

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    def test_identity_question_bypasses_semantic_state_selection(
        self,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_goal_status.return_value = "Goal `g1` status: running."
        mock_goal_cont.return_value = "Tracked task `t1` status: running."
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t3",
            user_text="Is this OpenClaw?",
            turn_plan=self._turn_plan(focus="none"),
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNone(result)
