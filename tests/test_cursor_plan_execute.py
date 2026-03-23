"""Tests for Cursor plan-first helpers."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.cursor_plan_execute import (  # noqa: E402
    extract_plan_text_from_conversation,
    plan_first_enabled,
    plan_text_usable,
    planner_model_for_lane,
)


class CursorPlanExecuteTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in (
            "ANDREA_CURSOR_PLAN_FIRST_ENABLED",
            "ANDREA_TELEGRAM_CURSOR_PLAN_FIRST",
            "ANDREA_REPAIR_CURSOR_PLAN_FIRST",
            "ANDREA_SELF_HEAL_CURSOR_PLAN_FIRST",
        ):
            os.environ.pop(key, None)

    def test_plan_first_enabled_lane_override(self) -> None:
        os.environ["ANDREA_TELEGRAM_CURSOR_PLAN_FIRST"] = "1"
        self.assertTrue(plan_first_enabled("telegram"))
        os.environ["ANDREA_TELEGRAM_CURSOR_PLAN_FIRST"] = "0"
        self.assertFalse(plan_first_enabled("telegram"))

    def test_plan_first_enabled_global(self) -> None:
        os.environ["ANDREA_CURSOR_PLAN_FIRST_ENABLED"] = "1"
        self.assertTrue(plan_first_enabled("repair"))

    def test_planner_model_lane_override(self) -> None:
        os.environ["ANDREA_TELEGRAM_CURSOR_PLANNER_MODEL"] = "custom-planner"
        self.assertEqual(planner_model_for_lane("telegram"), "custom-planner")

    def test_extract_plan_text_prefers_marker(self) -> None:
        conv = {
            "messages": [
                {"type": "assistant_message", "text": "preamble"},
                {
                    "type": "assistant_message",
                    "text": "## CursorExecutionPlan\n\n1. Edit `foo.py`\n2. Run tests\n",
                },
            ]
        }
        text = extract_plan_text_from_conversation(conv)
        self.assertIn("CursorExecutionPlan", text)
        self.assertIn("foo.py", text)

    def test_plan_text_usable(self) -> None:
        self.assertFalse(plan_text_usable("short"))
        long = "x" * 200
        self.assertTrue(plan_text_usable(long))
        self.assertTrue(plan_text_usable("## CursorExecutionPlan\n\n- step\n" + "y" * 50))
