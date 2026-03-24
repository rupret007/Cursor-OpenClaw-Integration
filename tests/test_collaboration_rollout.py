"""Unit tests for operator collaboration rollout layer."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.collaboration_rollout import (  # noqa: E402
    build_rollout_workspace,
    expansion_gate_report,
    operator_approve_live_advisory,
    record_scenario_onboarding,
    scenario_onboarding_blocks_live_advisory,
)
from services.andrea_sync.store import connect, migrate  # noqa: E402


class TestCollaborationRollout(unittest.TestCase):
    def setUp(self) -> None:
        fd, self._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = connect(Path(self._dbpath))
        migrate(self.conn)
        self._prev_promo = os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED")
        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._prev_promo is None:
            os.environ.pop("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", None)
        else:
            os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = self._prev_promo
        self.conn.close()
        Path(self._dbpath).unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(self._dbpath + suf).unlink(missing_ok=True)

    def _seed_useful_outcomes(self, n: int = 22) -> None:
        import time

        now = time.time()
        for i in range(n):
            self.conn.execute(
                """
                INSERT INTO collaboration_outcomes(
                    task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
                    canonical_class, usefulness_detail, live_advisory_ran, role_invocation_delta,
                    payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"t_ru_{i}",
                    "p_ru",
                    "s_ru",
                    f"c_ru_{i}",
                    "repoHelpVerified",
                    "verify_fail",
                    now,
                    "useful",
                    "useful_strategy_shift",
                    0,
                    2,
                    "{}",
                ),
            )
        self.conn.commit()

    def test_draft_scenario_rejects_live_onboarding(self) -> None:
        res = record_scenario_onboarding(
            self.conn,
            scenario_id="multiStepTroubleshoot",
            state="live_advisory",
            actor="tester",
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "draft_only_scenario_cannot_enter_live_advisory")

    def test_operator_approve_records_action_and_workspace(self) -> None:
        self._seed_useful_outcomes()
        res = operator_approve_live_advisory(
            self.conn,
            scenario_id="repoHelpVerified",
            trigger="verify_fail",
            actor="unit_tester",
            risk_notes="test",
        )
        self.assertTrue(res.get("ok"))
        ws = build_rollout_workspace(self.conn)
        self.assertTrue(any(
            str(x.get("actor")) == "unit_tester" for x in (ws.get("operator_actions_recent") or [])
        ))

    def test_expansion_gate_blocks_bounded_without_static_allowlist(self) -> None:
        self._seed_useful_outcomes()
        # advisory evidence ok but bounded gate requires static allowlist membership
        rep = expansion_gate_report(self.conn, "verificationSensitiveAction|trust_gate")
        self.assertFalse(rep.get("bounded_action_promotion_allowed"))

    def test_onboarding_shadow_blocks_live_advisory_approve(self) -> None:
        self._seed_useful_outcomes()
        record_scenario_onboarding(
            self.conn,
            scenario_id="verificationSensitiveAction",
            state="shadow_only",
            actor="tester",
        )
        self.assertTrue(
            scenario_onboarding_blocks_live_advisory(self.conn, "verificationSensitiveAction")
        )
        res = operator_approve_live_advisory(
            self.conn,
            scenario_id="verificationSensitiveAction",
            trigger="trust_gate",
            actor="tester",
            grant_subject=True,
            risk_notes="x",
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "scenario_onboarding_blocks_live")


if __name__ == "__main__":
    unittest.main()
