"""Tests for direct–state–delegate orchestration hints."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.orchestration_boundary import (  # noqa: E402
    build_decision_profile,
    is_cursor_worthy_heavy_lift,
    should_answer_before_delegate,
)


class TestOrchestrationBoundary(unittest.TestCase):
    def test_should_answer_before_delegate_status_queries(self) -> None:
        self.assertTrue(should_answer_before_delegate("What's blocked right now?"))
        self.assertTrue(
            should_answer_before_delegate("What happened with that task earlier?")
        )

    def test_should_answer_before_delegate_false_for_heavy_impl(self) -> None:
        self.assertFalse(
            should_answer_before_delegate(
                "What's blocked? Please refactor the auth service now."
            )
        )

    def test_build_decision_profile_force_delegate(self) -> None:
        p = build_decision_profile(
            "Fix the repo",
            turn_domain="technical_execution",
            scenario_force_delegate=True,
        )
        self.assertTrue(p.heavy_lift_hint)
        self.assertEqual(p.reason, "scenario_force_delegate")

    def test_is_cursor_worthy_heavy_lift(self) -> None:
        self.assertTrue(is_cursor_worthy_heavy_lift("Implement the OAuth2 flow"))
        self.assertFalse(is_cursor_worthy_heavy_lift("Where are we on the rollout?"))


if __name__ == "__main__":
    unittest.main()
