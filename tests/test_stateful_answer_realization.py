from __future__ import annotations

import os
import unittest
from unittest import mock

from services.andrea_sync.stateful_answer_realization import (
    _bundle_evidence_for_source,
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

