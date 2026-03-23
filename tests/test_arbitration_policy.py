"""Unit tests for bounded collaboration arbitration policy (v1)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.arbitration_policy import (  # noqa: E402
    build_collaboration_bundle,
    collaboration_budget,
    collaboration_layer_enabled,
    collaboration_summary_patch,
    should_attach_collaboration,
)
from services.andrea_sync.scenario_registry import get_contract  # noqa: E402


class ArbitrationPolicyTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in (
            "ANDREA_SYNC_COLLABORATION_LAYER",
            "ANDREA_SYNC_COLLAB_MAX_ROUNDS",
            "ANDREA_SYNC_COLLAB_MAX_ROLE_CALLS",
        ):
            os.environ.pop(key, None)

    def test_collaboration_layer_default_on(self) -> None:
        os.environ.pop("ANDREA_SYNC_COLLABORATION_LAYER", None)
        self.assertTrue(collaboration_layer_enabled())
        os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = "0"
        self.assertFalse(collaboration_layer_enabled())

    def test_collaboration_budget_env(self) -> None:
        os.environ["ANDREA_SYNC_COLLAB_MAX_ROUNDS"] = "3"
        os.environ["ANDREA_SYNC_COLLAB_MAX_ROLE_CALLS"] = "6"
        b = collaboration_budget()
        self.assertEqual(b["max_rounds_per_step"], 3)
        self.assertEqual(b["max_role_invocations"], 6)

    def test_should_attach_wrong_scenario(self) -> None:
        c = get_contract("repoHelpVerified")
        assert c is not None
        self.assertFalse(
            should_attach_collaboration(
                scenario_id="statusFollowupContinue",
                contract=c,
                trigger="verify_fail",
                plan_summary={},
            )
        )

    def test_should_attach_budget_exhausted(self) -> None:
        c = get_contract("repoHelpVerified")
        assert c is not None
        os.environ["ANDREA_SYNC_COLLAB_MAX_ROUNDS"] = "2"
        self.assertFalse(
            should_attach_collaboration(
                scenario_id="repoHelpVerified",
                contract=c,
                trigger="verify_fail",
                plan_summary={"collaboration": {"rounds": 2}},
            )
        )

    def test_build_bundle_none_when_layer_off(self) -> None:
        os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = "off"
        c = get_contract("repoHelpVerified")
        assert c is not None
        self.assertIsNone(
            build_collaboration_bundle(
                task_id="t1",
                goal_id="g1",
                plan_id="p1",
                step_id="s1",
                scenario_id="repoHelpVerified",
                contract=c,
                trigger="verify_fail",
                verdict="fail",
                verification_method="repo_checks",
                summary="no pr",
                lane="cursor",
                plan_summary={},
            )
        )

    def test_repo_help_verify_fail_switch_lane_cursor(self) -> None:
        c = get_contract("repoHelpVerified")
        assert c is not None
        bundle = build_collaboration_bundle(
            task_id="t1",
            goal_id="g1",
            plan_id="p1",
            step_id="s1",
            scenario_id="repoHelpVerified",
            contract=c,
            trigger="verify_fail",
            verdict="fail",
            verification_method="repo_checks",
            summary="missing pr",
            lane="cursor",
            plan_summary={},
            pr_url="",
        )
        self.assertIsNotNone(bundle)
        req, repair, arb, roles, _contrib = bundle  # type: ignore[misc]
        self.assertEqual(repair.strategy, "switch_lane")
        self.assertIn("openclaw_hybrid", repair.proof_plan)
        self.assertEqual(arb.repair_strategy, repair.strategy)
        self.assertTrue(any(r.role == "repair_strategist" for r in roles))
        patch = collaboration_summary_patch(
            {},
            request=req,
            repair=repair,
            arbitration=arb,
        )
        self.assertEqual(patch["collaboration"]["rounds"], 1)
        self.assertEqual(patch["collaboration"]["last_strategy"], "switch_lane")

    def test_trust_gate_ask_user(self) -> None:
        c = get_contract("verificationSensitiveAction")
        assert c is not None
        bundle = build_collaboration_bundle(
            task_id="t1",
            goal_id="g1",
            plan_id="p1",
            step_id="s1",
            scenario_id="verificationSensitiveAction",
            contract=c,
            trigger="trust_gate",
            verdict="pass",
            verification_method="human_confirm",
            summary="weak proof",
            lane="cursor",
            plan_summary={},
        )
        self.assertIsNotNone(bundle)
        _req, repair, _arb, _roles, _c = bundle  # type: ignore[misc]
        self.assertEqual(repair.strategy, "ask_user")
        self.assertIn("verification-sensitive", repair.proof_plan.lower())

    def test_second_round_escalation_hint(self) -> None:
        c = get_contract("repoHelpVerified")
        assert c is not None
        bundle = build_collaboration_bundle(
            task_id="t1",
            goal_id="g1",
            plan_id="p1",
            step_id="s1",
            scenario_id="repoHelpVerified",
            contract=c,
            trigger="verify_fail",
            verdict="fail",
            verification_method="repo_checks",
            summary="still bad",
            lane="cursor",
            plan_summary={"collaboration": {"rounds": 1}},
            pr_url="",
        )
        self.assertIsNotNone(bundle)
        _req, repair, _arb, _roles, _c = bundle  # type: ignore[misc]
        self.assertEqual(repair.strategy, "incident_escalation_hint")


if __name__ == "__main__":
    unittest.main()
