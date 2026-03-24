"""Unit tests for task-path advisory collaboration runtime."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.collaboration_runtime import (  # noqa: E402
    advisory_live_roles_eligible,
    collaboration_advisory_only,
    collaboration_action_enabled,
    collaboration_runtime_enabled,
    maybe_execute_bounded_collaboration_action,
    run_collaboration_round,
)
from services.andrea_sync.collaboration_schema import (  # noqa: E402
    ArbitrationDecision,
    RepairRecommendation,
    new_collaboration_id,
)


class CollaborationRuntimeTests(unittest.TestCase):
    def test_env_defaults(self) -> None:
        prev_r = os.environ.pop("ANDREA_SYNC_COLLAB_RUNTIME_ENABLED", None)
        prev_a = os.environ.pop("ANDREA_SYNC_COLLAB_ADVISORY_ONLY", None)
        prev_x = os.environ.pop("ANDREA_SYNC_COLLAB_ACTION_ENABLED", None)
        try:
            self.assertFalse(collaboration_runtime_enabled())
            self.assertTrue(collaboration_advisory_only())
            self.assertFalse(collaboration_action_enabled())
        finally:
            if prev_r is not None:
                os.environ["ANDREA_SYNC_COLLAB_RUNTIME_ENABLED"] = prev_r
            if prev_a is not None:
                os.environ["ANDREA_SYNC_COLLAB_ADVISORY_ONLY"] = prev_a
            if prev_x is not None:
                os.environ["ANDREA_SYNC_COLLAB_ACTION_ENABLED"] = prev_x

    def test_advisory_live_roles_eligible_gating(self) -> None:
        prev = os.environ.get("ANDREA_SYNC_COLLAB_RUNTIME_ENABLED")
        os.environ["ANDREA_SYNC_COLLAB_RUNTIME_ENABLED"] = "1"
        try:
            self.assertTrue(
                advisory_live_roles_eligible(
                    scenario_id="repoHelpVerified",
                    trigger="verify_fail",
                    plan_summary={},
                )
            )
            self.assertTrue(
                advisory_live_roles_eligible(
                    scenario_id="verificationSensitiveAction",
                    trigger="verify_fail",
                    plan_summary={},
                )
            )
        finally:
            if prev is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_RUNTIME_ENABLED", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_RUNTIME_ENABLED"] = prev

    def test_run_collaboration_round_arbitration(self) -> None:
        cid = new_collaboration_id()
        repair = RepairRecommendation(
            collab_id=cid,
            strategy="switch_lane",
            rationale="det",
            safe_scope="same",
            proof_plan="pp",
        )
        arb = ArbitrationDecision(
            collab_id=cid,
            decision="accept_repair_plan",
            chosen_contribution_id="",
            repair_strategy="switch_lane",
            trusted_to_continue=False,
        )

        def _fake(**kwargs: object) -> dict:
            role = str(kwargs.get("role") or "")
            if role == "triage":
                return {
                    "ok": True,
                    "provider": "p",
                    "model": "m1",
                    "payload": {
                        "recommended_strategy": "ask_user",
                        "analysis": "unsafe",
                        "confidence": 0.95,
                    },
                }
            return {
                "ok": True,
                "provider": "p",
                "model": "m2",
                "payload": {"accept_strategist": True, "issues": [], "recommended_override": "none"},
            }

        with mock.patch("services.andrea_sync.collaboration_runtime.run_role_json", side_effect=_fake):
            out = run_collaboration_round(
                task_id="t1",
                plan_id="pl1",
                collab_id=cid,
                scenario_id="repoHelpVerified",
                trigger="verify_fail",
                verdict="fail",
                outcome_summary="no pr",
                lane="cursor",
                deterministic_strategy="switch_lane",
                candidate_lanes=["cursor", "openclaw_hybrid"],
                pr_url="",
                agent_url="",
                proof_requirements="pr_url",
                repair=repair,
                arbitration=arb,
            )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["repair"].strategy, "ask_user")
        self.assertEqual(len(out["role_events"]), 2)

    def test_bounded_action_switch_lane(self) -> None:
        prev_a = os.environ.get("ANDREA_SYNC_COLLAB_ADVISORY_ONLY")
        prev_x = os.environ.get("ANDREA_SYNC_COLLAB_ACTION_ENABLED")
        prev_op = os.environ.get("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION")
        os.environ["ANDREA_SYNC_COLLAB_ADVISORY_ONLY"] = "0"
        os.environ["ANDREA_SYNC_COLLAB_ACTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION"] = "1"
        try:
            sp, rec, disp = maybe_execute_bounded_collaboration_action(
                scenario_id="repoHelpVerified",
                task_id="t1",
                plan_id="p1",
                collab_id="c1",
                final_strategy="switch_lane",
                lane="cursor",
                candidate_lanes=["cursor", "openclaw_hybrid"],
                target_lane_hint="openclaw_hybrid",
                plan_summary={"collaboration": {}},
                outcome_summary="x",
                trigger="verify_fail",
            )
            self.assertTrue(sp.get("collaboration", {}).get("bounded_action_executed"))
            self.assertEqual(rec.get("collaboration_action_executed", {}).get("type"), "switch_lane")
            self.assertIsNone(disp)
        finally:
            if prev_a is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_ADVISORY_ONLY", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_ADVISORY_ONLY"] = prev_a
            if prev_x is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_ACTION_ENABLED", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_ACTION_ENABLED"] = prev_x
            if prev_op is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION"] = prev_op


if __name__ == "__main__":
    unittest.main()
