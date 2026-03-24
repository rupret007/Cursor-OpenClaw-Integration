"""Tests for evidence-gated collaboration promotion controller."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.activation_policy import evaluate_activation_policy  # noqa: E402
from services.andrea_sync.collaboration_promotion import (  # noqa: E402
    evaluate_promotion_guardrails_after_outcome,
    get_promotion_activation_overlay,
    promote_subject_bounded_action,
    promote_subject_live_advisory,
)
from services.andrea_sync.store import connect, migrate  # noqa: E402


class CollaborationPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._td.name) / "t.db")
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._td.cleanup()

    def _seed_outcomes(
        self,
        *,
        scenario_id: str = "repoHelpVerified",
        trigger: str = "verify_fail",
        n: int = 22,
        klass: str = "useful",
    ) -> None:
        for i in range(n):
            self.conn.execute(
                """
                INSERT INTO collaboration_outcomes(
                    task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
                    canonical_class, usefulness_detail, live_advisory_ran, role_invocation_delta, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"t{i}",
                    "p",
                    "st",
                    f"c{i}",
                    scenario_id,
                    trigger,
                    float(i),
                    klass,
                    "x",
                    1,
                    2,
                    "{}",
                ),
            )
        self.conn.commit()

    def _seed_repairs(self, n: int = 12) -> None:
        for i in range(n):
            self.conn.execute(
                """
                INSERT INTO repair_outcomes(
                    task_id, plan_id, collab_id, ts, action_type, executed, payload_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (f"rt{i}", "p", f"cc{i}", float(i), "switch_lane", 1, "{}"),
            )
        self.conn.commit()

    def test_overlay_none_when_controller_off(self) -> None:
        prev = os.environ.pop("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", None)
        try:
            ov = get_promotion_activation_overlay(self.conn, "repoHelpVerified", "verify_fail")
            self.assertFalse(ov.get("promotion_controller_enabled"))
            self.assertIsNone(ov.get("effective_shadow_only"))
        finally:
            if prev is not None:
                os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = prev

    def test_promoted_live_sets_effective_shadow_false(self) -> None:
        prev_en = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        try:
            self._seed_outcomes(n=22, klass="useful")
            res = promote_subject_live_advisory(
                self.conn, scenario_id="repoHelpVerified", trigger="verify_fail", operator_ack=True
            )
            self.assertTrue(res.get("ok"), msg=str(res))
            ov = get_promotion_activation_overlay(self.conn, "repoHelpVerified", "verify_fail")
            self.assertFalse(ov.get("freeze_live_advisory"))
            self.assertIs(ov.get("effective_shadow_only"), False)
        finally:
            if prev_en is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = prev_en

    def test_activation_enforces_adaptive_when_promoted(self) -> None:
        prev_en = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        prev_sh = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY")
        prev_min = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE")
        prev_max = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE"] = "3"
        os.environ["ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE"] = "0.2"
        try:
            self._seed_outcomes(n=22, klass="useful")
            pr = promote_subject_live_advisory(
                self.conn, scenario_id="repoHelpVerified", trigger="verify_fail", operator_ack=True
            )
            self.assertTrue(pr.get("ok"), msg=str(pr))
            self._seed_outcomes(n=15, klass="wasteful")
            act = evaluate_activation_policy(
                conn=self.conn,
                task_id="tx",
                plan_id="px",
                step_id="sx",
                scenario_id="repoHelpVerified",
                trigger="verify_fail",
                verdict="fail",
                lane="cursor",
                collab_id="cx",
                collaboration_layer_on=True,
                will_attach_collaboration_bundle=True,
                attach_blocked_reasons=[],
                base_live_advisory_eligible=True,
                approval_blocked=False,
            )
            self.assertFalse(act.get("executed_live_advisory_planned"))
            self.assertEqual(act.get("activation_mode"), "record_only")
            self.assertIn("adaptive_suppress_live_advisory", act.get("reason_codes") or [])
        finally:
            for key, prev in (
                ("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", prev_en),
                ("ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY", prev_sh),
                ("ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE", prev_min),
                ("ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE", prev_max),
            ):
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev

    def test_rollback_on_harmful_outcome(self) -> None:
        prev_en = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        try:
            self._seed_outcomes(n=22, klass="useful")
            promote_subject_live_advisory(
                self.conn, scenario_id="repoHelpVerified", trigger="verify_fail", operator_ack=True
            )
            evaluate_promotion_guardrails_after_outcome(
                self.conn,
                scenario_id="repoHelpVerified",
                trigger="verify_fail",
                canonical_class="harmful",
            )
            ov = get_promotion_activation_overlay(self.conn, "repoHelpVerified", "verify_fail")
            self.assertNotEqual(ov.get("promotion_level"), "live_advisory")
        finally:
            if prev_en is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = prev_en

    def test_bounded_promotion_requires_persisted_revision_when_enabled(self) -> None:
        from services.andrea_sync.collaboration_runtime import maybe_execute_bounded_collaboration_action

        prev_en = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        prev_a = os.environ.get("ANDREA_SYNC_COLLAB_ADVISORY_ONLY")
        prev_x = os.environ.get("ANDREA_SYNC_COLLAB_ACTION_ENABLED")
        prev_op = os.environ.get("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_ADVISORY_ONLY"] = "0"
        os.environ["ANDREA_SYNC_COLLAB_ACTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION"] = "1"
        try:
            sp, _rec, _disp = maybe_execute_bounded_collaboration_action(
                conn=self.conn,
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
            self.assertEqual(sp, {})
        finally:
            for key, prev in (
                ("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", prev_en),
                ("ANDREA_SYNC_COLLAB_ADVISORY_ONLY", prev_a),
                ("ANDREA_SYNC_COLLAB_ACTION_ENABLED", prev_x),
                ("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION", prev_op),
            ):
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev

    def test_bounded_promotion_allows_matching_revision(self) -> None:
        from services.andrea_sync.collaboration_runtime import maybe_execute_bounded_collaboration_action

        prev_en = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        prev_a = os.environ.get("ANDREA_SYNC_COLLAB_ADVISORY_ONLY")
        prev_x = os.environ.get("ANDREA_SYNC_COLLAB_ACTION_ENABLED")
        prev_op = os.environ.get("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_ADVISORY_ONLY"] = "0"
        os.environ["ANDREA_SYNC_COLLAB_ACTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION"] = "1"
        try:
            self._seed_outcomes(n=22, klass="useful")
            self._seed_repairs(n=12)
            br = promote_subject_bounded_action(
                self.conn,
                scenario_id="repoHelpVerified",
                trigger="verify_fail",
                action_family="switch_lane",
                operator_ack=True,
            )
            self.assertTrue(br.get("ok"), msg=str(br))
            sp, rec, disp = maybe_execute_bounded_collaboration_action(
                conn=self.conn,
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
            for key, prev in (
                ("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", prev_en),
                ("ANDREA_SYNC_COLLAB_ADVISORY_ONLY", prev_a),
                ("ANDREA_SYNC_COLLAB_ACTION_ENABLED", prev_x),
                ("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION", prev_op),
            ):
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev


if __name__ == "__main__":
    unittest.main()
