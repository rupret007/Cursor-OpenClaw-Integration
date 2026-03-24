"""Tests for deterministic collaboration activation policy."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.activation_policy import (  # noqa: E402
    ACTIVATION_POLICY_VERSION,
    adaptive_recommends_suppress_live_advisory,
    collab_policy_shadow_only,
    evaluate_activation_policy,
    fetch_outcome_stats_for_pair,
    operator_action_promotion_confirmed,
)


class ActivationPolicyTests(unittest.TestCase):
    def test_evaluate_suppressed_when_layer_off(self) -> None:
        act = evaluate_activation_policy(
            conn=None,
            task_id="t1",
            plan_id="p1",
            step_id="s1",
            scenario_id="repoHelpVerified",
            trigger="verify_fail",
            verdict="fail",
            lane="cursor",
            collab_id="c1",
            collaboration_layer_on=False,
            will_attach_collaboration_bundle=True,
            attach_blocked_reasons=[],
            base_live_advisory_eligible=True,
            approval_blocked=False,
        )
        self.assertEqual(act["activation_mode"], "suppressed")
        self.assertIn("collaboration_layer_disabled", act["reason_codes"])

    def test_evaluate_advisory_when_eligible(self) -> None:
        act = evaluate_activation_policy(
            conn=None,
            task_id="t1",
            plan_id="p1",
            step_id="s1",
            scenario_id="repoHelpVerified",
            trigger="verify_fail",
            verdict="fail",
            lane="cursor",
            collab_id="c1",
            collaboration_layer_on=True,
            will_attach_collaboration_bundle=True,
            attach_blocked_reasons=[],
            base_live_advisory_eligible=True,
            approval_blocked=False,
        )
        self.assertEqual(act["activation_mode"], "advisory")
        self.assertTrue(act.get("action_candidate"))
        self.assertEqual(act["policy_version"], ACTIVATION_POLICY_VERSION)

    def test_adaptive_suppress_respects_shadow_default(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE collaboration_outcomes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              plan_id TEXT NOT NULL,
              step_id TEXT NOT NULL,
              collab_id TEXT NOT NULL,
              scenario_id TEXT NOT NULL,
              trigger TEXT NOT NULL,
              ts REAL NOT NULL,
              canonical_class TEXT NOT NULL,
              usefulness_detail TEXT NOT NULL DEFAULT '',
              live_advisory_ran INTEGER NOT NULL DEFAULT 0,
              role_invocation_delta INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        now = 1.0
        for i in range(15):
            conn.execute(
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
                    "repoHelpVerified",
                    "verify_fail",
                    now,
                    "wasteful",
                    "wasteful_roles_failed",
                    1,
                    2,
                    "{}",
                ),
            )
        conn.commit()

        prev_min = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE")
        prev_max = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE")
        prev_sh = os.environ.get("ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY")
        os.environ["ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE"] = "3"
        os.environ["ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE"] = "0.2"
        os.environ["ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY"] = "1"
        try:
            w, n = fetch_outcome_stats_for_pair(conn, scenario_id="repoHelpVerified", trigger="verify_fail")
            self.assertEqual(n, 15)
            self.assertEqual(w, 15)
            rec, meta = adaptive_recommends_suppress_live_advisory(
                conn, scenario_id="repoHelpVerified", trigger="verify_fail"
            )
            self.assertTrue(rec)
            self.assertTrue(meta.get("recommend_suppress"))
            act = evaluate_activation_policy(
                conn=conn,
                task_id="t1",
                plan_id="p1",
                step_id="s1",
                scenario_id="repoHelpVerified",
                trigger="verify_fail",
                verdict="fail",
                lane="cursor",
                collab_id="c1",
                collaboration_layer_on=True,
                will_attach_collaboration_bundle=True,
                attach_blocked_reasons=[],
                base_live_advisory_eligible=True,
                approval_blocked=False,
            )
            self.assertTrue(act.get("shadow_recommended_suppress_live"))
            self.assertTrue(collab_policy_shadow_only())
            self.assertTrue(act.get("executed_live_advisory_planned"))
            self.assertEqual(act["activation_mode"], "advisory")
        finally:
            if prev_min is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE"] = prev_min
            if prev_max is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE"] = prev_max
            if prev_sh is None:
                os.environ.pop("ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY", None)
            else:
                os.environ["ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY"] = prev_sh
        conn.close()

    def test_operator_action_promotion_default_off(self) -> None:
        prev = os.environ.pop("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION", None)
        try:
            self.assertFalse(operator_action_promotion_confirmed())
        finally:
            if prev is not None:
                os.environ["ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION"] = prev


if __name__ == "__main__":
    unittest.main()
