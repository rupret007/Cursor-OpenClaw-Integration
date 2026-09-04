"""Fail-closed dashboard coverage for offline doctor handoff receipts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.andrea_doctor_receipt import build_receipt, write_receipt
from services.andrea_sync.dashboard import (
    OPERATOR_RECEIPT_MAX_AGE_SECONDS,
    OPERATOR_RECEIPT_REFRESH_COMMAND,
    build_operator_readiness_snapshot,
)


class _FakeServer:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root


def _readiness(*, grade: str = "A") -> dict[str, object]:
    return {
        "grade": grade,
        "readiness_plan": {
            "safe_for_autonomous_ops": grade == "A",
            "blocker_count": 0 if grade == "A" else 1,
            "who_acts_first": "coding_agent" if grade == "A" else "owner",
            "next_action": "Continue the assigned offline product test.",
            "andrea_next_action": "Continue offline checks only.",
            "coding_agent_next_action": "Run the focused offline test suite.",
            "owner_next_action": "Review the named readiness blocker.",
            "holds": ["Do not send any live message."],
            "routing": {
                "andrea": "offline only",
                "coding_agent": "offline code and tests only",
                "owner": "owner-gated actions only",
            },
            "actions": [],
        },
    }


class TestDashboardOperatorReadiness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.server = _FakeServer(self.repo_root)
        self.receipt_path = self.repo_root / "data" / "andrea-doctor-receipt.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(
        self,
        *,
        grade: str = "A",
        security: str = "passed",
        reliability: str = "passed",
        exit_code: int = 0,
    ) -> float:
        receipt = build_receipt(
            _readiness(grade=grade),
            security_status=security,
            reliability_status=reliability,
            openclaw_status="skipped_offline",
            exit_code=exit_code,
        )
        write_receipt(self.receipt_path, receipt)
        return self.receipt_path.stat().st_mtime

    def test_missing_receipt_blocks_but_names_safe_offline_refresh(self) -> None:
        snapshot = build_operator_readiness_snapshot(self.server, now=100.0)

        self.assertEqual(snapshot["receipt_state"], "missing")
        self.assertEqual(snapshot["overall_status"], "blocked")
        self.assertFalse(snapshot["trusted_receipt"])
        self.assertFalse(snapshot["safe_for_autonomous_ops"])
        self.assertTrue(snapshot["may_continue_offline_code"])
        self.assertEqual(snapshot["who_acts_first"], "coding_agent")
        self.assertIn(OPERATOR_RECEIPT_REFRESH_COMMAND, snapshot["next_action"])
        self.assertNotIn(str(self.repo_root), json.dumps(snapshot))

    def test_current_ready_receipt_drives_actor_and_next_action(self) -> None:
        modified_at = self._write()
        snapshot = build_operator_readiness_snapshot(
            self.server, now=modified_at + 90.0
        )

        self.assertEqual(snapshot["receipt_state"], "current")
        self.assertTrue(snapshot["trusted_receipt"])
        self.assertTrue(snapshot["receipt_verified"])
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["overall_status"], "ready")
        self.assertEqual(snapshot["who_acts_first"], "coding_agent")
        self.assertEqual(snapshot["next_action"], "Continue the assigned offline product test.")
        self.assertEqual(snapshot["age_seconds"], 90.0)
        self.assertNotIn("receipt_fingerprint", snapshot)

    def test_current_failed_stage_stays_owner_blocked(self) -> None:
        modified_at = self._write(security="failed", exit_code=1)
        snapshot = build_operator_readiness_snapshot(
            self.server, now=modified_at + 5.0
        )

        self.assertEqual(snapshot["receipt_state"], "current")
        self.assertTrue(snapshot["trusted_receipt"])
        self.assertEqual(snapshot["overall_status"], "blocked")
        self.assertEqual(snapshot["who_acts_first"], "owner")
        self.assertTrue(snapshot["must_wait_for_owner"])
        self.assertFalse(snapshot["may_continue_offline_code"])
        self.assertEqual(snapshot["failed_stages"], ["security"])
        self.assertIn("Doctor stage failed (security)", snapshot["next_action"])

    def test_stale_receipt_cannot_authorize_current_operation(self) -> None:
        modified_at = self._write()
        snapshot = build_operator_readiness_snapshot(
            self.server,
            now=modified_at + OPERATOR_RECEIPT_MAX_AGE_SECONDS + 1,
        )

        self.assertEqual(snapshot["receipt_state"], "stale")
        self.assertTrue(snapshot["receipt_verified"])
        self.assertFalse(snapshot["trusted_receipt"])
        self.assertEqual(snapshot["overall_status"], "blocked")
        self.assertFalse(snapshot["safe_for_autonomous_ops"])
        self.assertIn(OPERATOR_RECEIPT_REFRESH_COMMAND, snapshot["next_action"])

    def test_tampered_receipt_fails_closed_without_leaking_raw_fields(self) -> None:
        self._write()
        payload = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        payload["probe_notes"] = "SECRET raw probe output"
        self.receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(self.receipt_path, 0o600)

        snapshot = build_operator_readiness_snapshot(self.server)

        self.assertEqual(snapshot["receipt_state"], "invalid")
        self.assertFalse(snapshot["receipt_verified"])
        self.assertFalse(snapshot["trusted_receipt"])
        self.assertEqual(snapshot["who_acts_first"], "owner")
        self.assertTrue(snapshot["must_wait_for_owner"])
        self.assertEqual(snapshot["reason"], "unexpected_keys")
        self.assertNotIn("SECRET", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
