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
    def test_prefers_non_thin_recap_over_grace_fallback(
        self,
        mock_recent: mock.MagicMock,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_recent.return_value = (
            "I'm not finding a strong stored summary from the recent Cursor work yet."
        )
        mock_goal_status.return_value = None
        mock_goal_cont.return_value = "Cursor recap: Added retries and fixed timeout handling."

        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t1",
            user_text="What did Cursor say?",
            turn_plan=self._turn_plan(focus="recent_outcome_history"),
            scenario_id="statusFollowupContinue",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "goal_continuity")
        self.assertIn("Cursor recap:", result.reply_text)
        self.assertGreaterEqual(result.score, 70)

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
