"""Routing matrix eval: scenario catalog, routing contracts, export row shape."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.conversation_eval import (  # noqa: E402
    ROUTING_MATRIX_CASES,
    ROUTING_MATRIX_SMOKE_CASE_IDS,
    build_routing_matrix_export_row,
    collect_routing_contract_findings,
    routing_matrix_scenarios,
)
from services.andrea_sync.experience_types import ExperienceScenario  # noqa: E402


class RoutingMatrixEvalTests(unittest.TestCase):
    def test_routing_matrix_full_catalog_size(self) -> None:
        scenarios = routing_matrix_scenarios({})
        self.assertEqual(len(scenarios), len(ROUTING_MATRIX_CASES))

    def test_routing_matrix_scenario_ids(self) -> None:
        for s in routing_matrix_scenarios({}):
            self.assertIsInstance(s, ExperienceScenario)
            self.assertTrue(str(s.scenario_id).startswith("routing_matrix::"))

    def test_routing_matrix_smoke_subset(self) -> None:
        smoke = routing_matrix_scenarios({"smoke": True})
        ids = {s.scenario_id for s in smoke}
        for cid in ROUTING_MATRIX_SMOKE_CASE_IDS:
            self.assertIn(f"routing_matrix::{cid}", ids)
        self.assertEqual(len(smoke), len(ROUTING_MATRIX_SMOKE_CASE_IDS))

    def test_collect_routing_contract_assistant_route_mismatch(self) -> None:
        case = next(c for c in ROUTING_MATRIX_CASES if c.case_id == "rm_control_cancel_mock")
        cap = {
            "assistant_route": "delegate",
            "task_status": "completed",
            "routing_execution_lane": "",
            "routing_task_meta": {},
        }
        hits = collect_routing_contract_findings(case, cap, user_text=case.turns[0])
        codes = {h["issue_code"] for h in hits}
        self.assertIn("routing_contract_assistant_route", codes)

    def test_build_routing_matrix_export_row_shape(self) -> None:
        case = ROUTING_MATRIX_CASES[0]
        cap = {
            "rendered_reply_sanitized": "hello",
            "assistant_route": "direct",
            "assistant_reason": "test_reason",
            "turn_plan_domain": "casual_conversation",
            "turn_plan_continuity_focus": "none",
            "task_status": "completed",
            "routing_event_type_counts": {"USER_MESSAGE": 1},
            "routing_execution_lane": "",
            "harness_timing": {"submit_ms": 1.0},
        }
        detail = {"task": {"meta": {"execution": {}, "openclaw": {}, "cursor": {}}}, "events": []}
        row = build_routing_matrix_export_row(
            scenario_id="routing_matrix::rm_casual_hows",
            case=case,
            turn_index=0,
            user_text="hi",
            cap=cap,
            detail=detail,
        )
        self.assertEqual(row["case_id"], case.case_id)
        self.assertIn("event_type_counts", row)
        self.assertIn("harness_timing", row)

if __name__ == "__main__":
    unittest.main()
