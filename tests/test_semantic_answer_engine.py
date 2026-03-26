from __future__ import annotations

import unittest
from unittest import mock

from services.andrea_sync.semantic_answer_engine import (
    brevity_profile_for_answer_mode,
    choose_semantic_state_reply,
)
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

        for text in (
            "What did Cursor say?",
            "What happened to the Cursor thread?",
            "What happened with Cursor?",
        ):
            result = choose_semantic_state_reply(
                conn=object(),
                task_id="t1",
                user_text=text,
                turn_plan=self._turn_plan(focus="recent_outcome_history"),
                scenario_id="statusFollowupContinue",
            )

            self.assertIsNotNone(result, msg=text)
            assert result is not None
            self.assertEqual(result.source, "cursor_continuity_recall", msg=text)
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
        meta = result.to_metadata()
        self.assertIn("turn_contract", meta)
        contract = meta.get("turn_contract")
        self.assertIsInstance(contract, dict)
        assert isinstance(contract, dict)
        self.assertEqual(contract.get("family"), "cursor_recall")
        self.assertEqual(contract.get("source"), "cursor_continuity_recall")
        self.assertIn("cursor", contract.get("required_anchors") or [])
        self.assertIn(contract.get("answer_mode") or "", ("strong_evidence_answer", "partial_evidence_helpful_answer"))
        self.assertIsInstance(contract.get("next_step_options"), list)

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

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    def test_approval_family_excludes_goal_continuity_source(
        self,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        approval_turn_plan = TurnPlan(
            domain="approval_state",
            context_boundary="approval_and_plan_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=False,
            continuity_focus="none",
        )
        mock_goal_status.return_value = "Pending approvals for tracked task `t1`: **2**."
        mock_goal_cont.return_value = "Goal `g1` — tracked task `t1` status: running."
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-approval",
            user_text="What still needs approval?",
            turn_plan=approval_turn_plan,
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "goal_status")
        self.assertEqual(result.family, "approval_state")

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    def test_approval_contract_carries_required_anchor(
        self,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        approval_turn_plan = TurnPlan(
            domain="approval_state",
            context_boundary="approval_and_plan_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=False,
            continuity_focus="none",
        )
        mock_goal_status.return_value = (
            "Pending approvals for tracked task `t1`: 2.\n"
            "Top pending item is awaiting final signoff from release owner."
        )
        mock_goal_cont.return_value = None
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-approval-anchor",
            user_text="What still needs approval?",
            turn_plan=approval_turn_plan,
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNotNone(result)
        assert result is not None
        contract = result.to_metadata().get("turn_contract") or {}
        self.assertIn("approval", contract.get("required_anchors") or [])

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    def test_non_stateful_text_guard_blocks_stateful_hijack(
        self,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_goal_status.return_value = "Goal `g1` status: running."
        mock_goal_cont.return_value = "Tracked task `t1` status: running."
        forced_status_plan = TurnPlan(
            domain="project_status",
            context_boundary="project_continuity_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=False,
            continuity_focus="recent_outcome_history",
        )
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-agenda",
            user_text="What's on the agenda today?",
            turn_plan=forced_status_plan,
            scenario_id="statusFollowupContinue",
        )
        self.assertIsNone(result)

    @mock.patch("services.andrea_sync.semantic_answer_engine.build_goal_continuity_reply")
    @mock.patch("services.andrea_sync.semantic_answer_engine.try_goal_status_nl_reply")
    @mock.patch(
        "services.andrea_sync.semantic_answer_engine.build_recent_outcome_history_reply_from_state"
    )
    def test_family_override_and_allowed_sources_bind_anaphoric_selection(
        self,
        mock_recent: mock.MagicMock,
        mock_goal_status: mock.MagicMock,
        mock_goal_cont: mock.MagicMock,
    ) -> None:
        mock_recent.return_value = "Cursor recap: changed the migration ordering and fixed retries."
        mock_goal_status.return_value = "Goal `g1` status: running."
        mock_goal_cont.return_value = "Goal continuity details."
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-anaphor",
            user_text="What happened there?",
            turn_plan=self._turn_plan(focus="recent_outcome_history"),
            scenario_id="statusFollowupContinue",
            family_override="cursor_recall",
            allowed_sources_override=("cursor_continuity_recall",),
            binding_reason="anaphoric_outcome_same_chat",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "cursor_continuity_recall")
        contract = result.to_metadata().get("turn_contract") or {}
        self.assertEqual(contract.get("family"), "cursor_recall")
        self.assertEqual(contract.get("allowed_sources"), ["cursor_continuity_recall"])
        self.assertEqual(contract.get("binding_reason"), "anaphoric_outcome_same_chat")

    def test_stateful_allowed_false_abstains_even_with_stateful_turn_plan(self) -> None:
        result = choose_semantic_state_reply(
            conn=object(),
            task_id="t-veto",
            user_text="What do you think about that?",
            turn_plan=self._turn_plan(focus="recent_outcome_history"),
            scenario_id="statusFollowupContinue",
            stateful_allowed=False,
            binding_reason="non_stateful_turn:opinion_reflection",
        )
        self.assertIsNone(result)

    def test_brevity_profile_for_answer_mode(self) -> None:
        g, n = brevity_profile_for_answer_mode("strong_evidence_answer")
        self.assertEqual(g, "concise_grounded_summary")
        self.assertEqual(n, 115)
        g2, n2 = brevity_profile_for_answer_mode("partial_evidence_helpful_answer")
        self.assertEqual(g2, "partial_helpful_brevity")
        self.assertEqual(n2, 185)
        g3, n3 = brevity_profile_for_answer_mode("truthful_fallback_with_next_steps")
        self.assertEqual(g3, "truthful_next_steps_brevity")
        self.assertEqual(n3, 260)
