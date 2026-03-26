from __future__ import annotations

import os
import unittest
from unittest import mock

from services.andrea_sync.stateful_answer_realization import (
    _bundle_evidence_for_source,
    _compress_reply_to_word_budget,
    maybe_realize_grounded_technical_reply,
    maybe_realize_stateful_reply,
)
from services.andrea_sync.turn_intelligence import TurnPlan


class StatefulAnswerRealizationTests(unittest.TestCase):
    def _turn_plan(self) -> TurnPlan:
        return TurnPlan(
            domain="project_status",
            context_boundary="project_continuity_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=True,
            continuity_focus="recent_outcome_history",
        )

    def test_returns_none_when_disabled(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "0",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="goal_status",
                deterministic_reply="Goal g1 is blocked on tests.",
                fallback_reply="Goal status unavailable.",
                user_text="status?",
                turn_plan=self._turn_plan(),
            )
        self.assertIsNone(out)

    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_grounded_technical_realization_requires_evidence_anchors(
        self, mock_chat: mock.MagicMock
    ) -> None:
        mock_chat.return_value = {
            "reply": "Timeout errors are commonly transient; bounded retries with backoff usually help.",
            "grounded": True,
            "used_fallback": False,
            "anchors_used": ["timeout", "retries"],
        }
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "ANDREA_GROUNDED_RESEARCH_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_grounded_technical_reply(
                user_text="What does this timeout error usually mean?",
                answer_family="grounded_research",
                evidence_lines=[
                    "Timeout failures are often transient under network jitter.",
                    "Retries with bounded backoff reduce repeated timeout failures.",
                ],
                fallback_reply="I can only confirm that retries may help some timeout failures.",
                required_anchors=("timeout", "retries"),
                evidence_strength=5,
            )
        self.assertIsNotNone(out)
        assert out is not None
        low = out.lower()
        self.assertIn("timeout", low)
        self.assertIn("retries", low)

    def test_bundle_goal_status_splits_structured_lines(self) -> None:
        evidence = _bundle_evidence_for_source(
            conn=object(),
            task_id="t-evidence",
            source="goal_status",
            user_text="What still needs approval?",
            deterministic_reply=(
                "Pending approvals for tracked task `tsk_1`: **2**.\n"
                "Top pending approval: `appr_1` - confirm the deploy window."
            ),
        )
        self.assertGreaterEqual(len(evidence), 2)
        self.assertTrue(any("pending approvals" in line.lower() for line in evidence))
        self.assertTrue(any("deploy window" in line.lower() for line in evidence))

    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_uses_grounded_realized_text(self, mock_chat: mock.MagicMock) -> None:
        mock_chat.return_value = {
            "reply": "Goal g1 is blocked on tests and waiting for your confirmation.",
            "grounded": True,
            "used_fallback": False,
        }
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="goal_status",
                deterministic_reply="Goal g1 is blocked on tests.",
                fallback_reply="Goal status unavailable.",
                user_text="what's blocked?",
                turn_plan=self._turn_plan(),
            )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("blocked on tests", out.lower())

    @mock.patch("services.andrea_sync.stateful_answer_realization._bundle_evidence_for_source")
    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_recall_contamination_forces_fallback(
        self,
        mock_chat: mock.MagicMock,
        mock_bundle: mock.MagicMock,
    ) -> None:
        mock_bundle.return_value = ["Cursor completed auth hardening and tests."]
        mock_chat.return_value = {
            "reply": "Cursor recap: Status / follow-up reply (goal_runtime_status).",
            "grounded": True,
            "used_fallback": False,
        }
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="cursor_continuity_recall",
                deterministic_reply="Cursor recap: auth hardening shipped.",
                fallback_reply="I’m not finding a recent clean Cursor result to recap from this thread.",
                user_text="What did Cursor say?",
                turn_plan=self._turn_plan(),
            )
        self.assertEqual(
            out,
            "I’m not finding a recent clean Cursor result to recap from this thread.",
        )

    @mock.patch("services.andrea_sync.stateful_answer_realization._bundle_evidence_for_source")
    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_rejects_fallback_shaped_realization_when_evidence_is_strong(
        self,
        mock_chat: mock.MagicMock,
        mock_bundle: mock.MagicMock,
    ) -> None:
        mock_bundle.return_value = [
            "Latest useful result: Cursor completed rollout and smoke checks cleanly.",
            "Recent receipt (outcome): Rollout completed with zero regressions in staging.",
            "Phase summary: Final verification and release notes published.",
        ]
        mock_chat.return_value = {
            "reply": "I’m not finding a recent clean Cursor result to recap from this thread.",
            "grounded": True,
            "used_fallback": False,
        }
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="cursor_continuity_recall",
                deterministic_reply="Cursor recap: rollout and smoke checks completed.",
                fallback_reply="I’m not finding a recent clean Cursor result to recap from this thread.",
                user_text="What did Cursor say?",
                turn_plan=self._turn_plan(),
            )
        self.assertIsNone(out)

    @mock.patch("services.andrea_sync.stateful_answer_realization._bundle_evidence_for_source")
    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_approval_realization_must_keep_approval_anchor(
        self,
        mock_chat: mock.MagicMock,
        mock_bundle: mock.MagicMock,
    ) -> None:
        mock_bundle.return_value = [
            "Pending approvals for tracked task `t1`: 2.",
            "Top pending approval: `appr_1` - confirm deploy window.",
        ]
        mock_chat.return_value = {
            "reply": "Two items are still pending right now.",
            "grounded": True,
            "used_fallback": False,
        }
        approval_turn_plan = TurnPlan(
            domain="approval_state",
            context_boundary="approval_and_plan_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=True,
            continuity_focus="none",
        )
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="goal_status",
                deterministic_reply="Pending approvals for tracked task `t1`: 2.",
                fallback_reply="I'm not seeing any approval requests waiting on you right now.",
                user_text="What still needs approval?",
                turn_plan=approval_turn_plan,
            )
        self.assertIsNone(out)

    @mock.patch("services.andrea_sync.stateful_answer_realization._bundle_evidence_for_source")
    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_contract_anchor_requires_reply_or_anchor_usage(
        self,
        mock_chat: mock.MagicMock,
        mock_bundle: mock.MagicMock,
    ) -> None:
        mock_bundle.return_value = [
            "Pending approvals for tracked task `t1`: 2.",
            "Top pending approval: `appr_1` - confirm deploy window.",
        ]
        mock_chat.return_value = {
            "reply": "Two items are pending right now.",
            "grounded": True,
            "used_fallback": False,
            "anchors_used": ["pending"],
        }
        approval_turn_plan = TurnPlan(
            domain="approval_state",
            context_boundary="approval_and_plan_state",
            prefer_state_reply=True,
            force_delegate=False,
            should_repair_generic=True,
            allow_goal_continuity_repair=True,
            inject_durable_memory=True,
            continuity_focus="none",
        )
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="goal_status",
                deterministic_reply="Pending approvals for tracked task `t1`: 2.",
                fallback_reply="I'm not seeing any approval requests waiting on you right now.",
                user_text="What still needs approval?",
                turn_plan=approval_turn_plan,
                turn_contract={
                    "family": "approval_state",
                    "source": "goal_status",
                    "required_anchors": ["approval"],
                    "evidence_lines": mock_bundle.return_value,
                    "evidence_strength": 6,
                    "fallback_policy": "prefer_grounded_specifics_then_truthful_fallback",
                },
            )
        self.assertIsNone(out)

    @mock.patch("services.andrea_sync.stateful_answer_realization._bundle_evidence_for_source")
    @mock.patch("services.andrea_sync.stateful_answer_realization._openai_json_chat")
    def test_partial_contract_appends_next_steps_when_llm_omits_them(
        self,
        mock_chat: mock.MagicMock,
        mock_bundle: mock.MagicMock,
    ) -> None:
        mock_bundle.return_value = [
            "Latest useful result: tightened timeout handling for the worker.",
        ]
        mock_chat.return_value = {
            "reply": "Cursor recap: latest useful result tightened timeout handling for the worker.",
            "grounded": True,
            "used_fallback": False,
        }
        with mock.patch.dict(
            os.environ,
            {
                "ANDREA_STATEFUL_REALIZATION_ENABLED": "1",
                "OPENAI_API_ENABLED": "1",
                "OPENAI_API_KEY": "x",
            },
            clear=False,
        ):
            out = maybe_realize_stateful_reply(
                conn=object(),
                task_id="t1",
                source="cursor_continuity_recall",
                deterministic_reply="Cursor recap: tightened timeout handling for the worker.",
                fallback_reply="I’m not finding a recent clean Cursor result to recap from this thread.",
                user_text="Where are we on the project overall?",
                turn_plan=self._turn_plan(),
                turn_contract={
                    "family": "cursor_recall",
                    "source": "cursor_continuity_recall",
                    "required_anchors": ["cursor"],
                    "evidence_lines": list(mock_bundle.return_value),
                    "evidence_strength": 4,
                    "fallback_policy": "allow_truthful_fallback_when_evidence_thin",
                    "answer_mode": "partial_evidence_helpful_answer",
                    "uncertainty_mode": "partial",
                    "next_step_options": [
                        "Narrow to the exact toolkit version and error string you are seeing.",
                        "Re-run grounded lookup after you capture the precise stack trace.",
                    ],
                },
            )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("next options", out.lower())
        self.assertIn("toolkit", out.lower())

    def test_compress_reply_respects_word_budget_and_evidence(self) -> None:
        ev = ("The migration ordering was fixed and retries now cap at three.",)
        long_reply = (
            "The migration ordering was fixed and retries now cap at three. "
            "We also adjusted handler paths for edge cases around timeouts. "
            "Finally we documented the rollout risk for operators."
        )
        out = _compress_reply_to_word_budget(
            long_reply,
            max_words=16,
            evidence_lines=ev,
            required_anchors=(),
        )
        self.assertLessEqual(len(out.split()), 22)
        self.assertIn("migration", out.lower())

