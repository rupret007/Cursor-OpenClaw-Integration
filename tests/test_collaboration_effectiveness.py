"""Tests for collaboration usefulness normalization and rollups."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.collaboration_effectiveness import (  # noqa: E402
    build_collaboration_outcome_payload,
    canonical_usefulness_class,
    rollup_collaboration_policy_profiles,
)


class CollaborationEffectivenessTests(unittest.TestCase):
    def test_canonical_usefulness_mapping(self) -> None:
        self.assertEqual(canonical_usefulness_class("useful_strategy_shift"), "useful")
        self.assertEqual(canonical_usefulness_class("wasteful_no_alternate_lane"), "wasteful")
        self.assertEqual(canonical_usefulness_class("informational_no_shift"), "informational")

    def test_outcome_payload_has_canonical_class(self) -> None:
        p = build_collaboration_outcome_payload(
            task_id="t",
            goal_id="g",
            plan_id="p",
            step_id="s",
            collab_id="c",
            scenario_id="repoHelpVerified",
            trigger="verify_fail",
            verdict_before="fail",
            verification_method="x",
            advisory_source="live_advisory",
            usefulness_detail="useful_safety_escalation",
            final_strategy="ask_user",
            bounded_action_type="",
            live_advisory_ran=True,
            role_invocation_delta=2,
        )
        self.assertEqual(p["canonical_class"], "useful")

    def test_rollup_profiles(self) -> None:
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
            CREATE TABLE collaboration_activation_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              plan_id TEXT NOT NULL,
              step_id TEXT NOT NULL,
              collab_id TEXT NOT NULL,
              scenario_id TEXT NOT NULL,
              trigger TEXT NOT NULL,
              ts REAL NOT NULL,
              activation_mode TEXT NOT NULL,
              policy_version TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO collaboration_outcomes(
                task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
                canonical_class, usefulness_detail, live_advisory_ran, role_invocation_delta, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("t", "p", "s", "c", "repoHelpVerified", "verify_fail", 1.0, "wasteful", "x", 1, 2, "{}"),
        )
        conn.execute(
            """
            INSERT INTO collaboration_activation_decisions(
                task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
                activation_mode, policy_version, payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            ("t", "p", "s", "c", "repoHelpVerified", "verify_fail", 1.0, "advisory", "v1", "{}"),
        )
        conn.commit()
        roll = rollup_collaboration_policy_profiles(conn)
        self.assertTrue(roll.get("ok"))
        self.assertEqual(len(roll.get("scenario_profiles") or []), 1)
        self.assertIn("advisory", roll.get("activation_counts") or {})
        conn.close()


if __name__ == "__main__":
    unittest.main()
