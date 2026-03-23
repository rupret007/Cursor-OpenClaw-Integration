"""Tests for trust-first background autonomy and delegated lifecycle helpers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.dashboard import _build_background_autonomy_summary  # noqa: E402
from services.andrea_sync.delegated_lifecycle import build_delegated_lifecycle_contract  # noqa: E402
from services.andrea_sync.resource_vocabulary import infer_resource_lane  # noqa: E402
from services.andrea_sync.store import connect, migrate, save_experience_run  # noqa: E402


class AndreaTrustFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(str(self.db_path) + suf).unlink(missing_ok=True)

    def test_build_delegated_lifecycle_contract_finished_agent(self) -> None:
        c = build_delegated_lifecycle_contract(
            {
                "openclaw": {"session_id": "sess1", "run_id": "run1"},
                "cursor": {
                    "agent_id": "ag1",
                    "terminal_status": "FINISHED",
                    "agent_url": "https://example.com/a",
                },
                "execution": {
                    "execution_lane": "openclaw_hybrid",
                    "delegated_to_cursor": True,
                },
                "outcome": {"current_phase": "synthesis", "completed_phases": ["plan"]},
            }
        )
        self.assertEqual(c["contract_version"], 1)
        self.assertEqual(c["cursor"]["agent_id"], "ag1")
        self.assertIn("conversation", c["recommended_next_actions"])

    def test_infer_resource_lane_openclaw(self) -> None:
        lane = infer_resource_lane({"execution_lane": "openclaw_hybrid"})
        self.assertEqual(lane, "openclaw_hybrid")

    def test_background_autonomy_summary_reflects_experience_run(self) -> None:
        save_experience_run(
            self.conn,
            {
                "run_id": "exp_trust_1",
                "actor": "test",
                "status": "completed",
                "passed": True,
                "total_checks": 2,
                "checks": [{"scenario_id": "a", "passed": True}],
            },
        )
        summary = _build_background_autonomy_summary(self.conn)
        self.assertEqual(summary.get("experience_run_id"), "exp_trust_1")
        self.assertTrue(summary.get("fresh"))


if __name__ == "__main__":
    unittest.main()
