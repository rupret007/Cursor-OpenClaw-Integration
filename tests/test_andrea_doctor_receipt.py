"""Tests for the offline doctor's redaction-safe machine handoff receipt."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPT_SCRIPT = REPO_ROOT / "scripts" / "andrea_doctor_receipt.py"
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "andrea_doctor.sh"


def _load_receipt_module():
    spec = importlib.util.spec_from_file_location(
        "andrea_doctor_receipt", str(RECEIPT_SCRIPT)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _readiness(grade: str = "A") -> dict:
    return {
        "grade": grade,
        "reasons": [],
        "summary": {"blocked": 0},
        "repo_root": "/private/path-that-must-not-be-copied",
        "probe_notes": "raw-secret-marker",
        "readiness_plan": {
            "safe_for_autonomous_ops": grade != "C",
            "blocker_count": 0 if grade != "C" else 1,
            "who_acts_first": "coding_agent" if grade == "A" else "owner",
            "next_action": "Continue the assigned offline test.",
            "andrea_next_action": "Keep the draft pending.",
            "coding_agent_next_action": "Run offline verification.",
            "owner_next_action": "No owner setup is required.",
            "holds": ["Do not send any live message.", "Keep Private API off."],
            "routing": {
                "andrea": "Handle operator replies.",
                "coding_agent": "Handle offline code and tests.",
                "owner": "Approve owner-only actions.",
            },
            "actions": [],
        },
    }


class TestAndreaDoctorReceipt(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = _load_receipt_module()

    def test_ready_receipt_has_stable_fingerprint_and_allowlisted_fields(self) -> None:
        first = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        second = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        self.assertEqual(first["overall_status"], "ready")
        self.assertTrue(first["handoff"]["safe_for_autonomous_ops"])
        self.assertEqual(first["handoff"]["who_acts_first"], "coding_agent")
        self.assertEqual(first["receipt_fingerprint"], second["receipt_fingerprint"])
        self.assertRegex(first["receipt_fingerprint"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(first)
        self.assertNotIn("raw-secret-marker", serialized)
        self.assertNotIn("private/path-that-must-not-be-copied", serialized)
        self.assertNotIn("probe_notes", serialized)
        self.assertIn("--offline --receipt", first["commands"]["rerun"])

    def test_failed_stage_forces_blocked_even_when_grade_is_a(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="not_run",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertFalse(receipt["handoff"]["safe_for_autonomous_ops"])
        self.assertEqual(receipt["stages"]["security"]["status"], "failed")

    def test_missing_readiness_fails_closed_to_owner(self) -> None:
        receipt = self._mod.build_receipt(
            {},
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertEqual(receipt["stages"]["readiness"]["grade"], "C")
        self.assertEqual(receipt["handoff"]["who_acts_first"], "owner")
        self.assertIn(
            "Restore a valid readiness receipt", receipt["handoff"]["next_action"]
        )

    def test_cli_writes_atomic_private_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = root / "readiness.json"
            receipt_path = root / "receipt.json"
            readiness_path.write_text(json.dumps(_readiness("B")), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--readiness-json",
                    str(readiness_path),
                    "--security-status",
                    "passed",
                    "--reliability-status",
                    "passed",
                    "--openclaw-status",
                    "skipped_offline",
                    "--exit-code",
                    "0",
                    "--output",
                    str(receipt_path),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["overall_status"], "ready_with_limits")
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_exact_offline_doctor_writes_complete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = Path(temp_dir) / "doctor.json"
            proc = subprocess.run(
                [
                    "bash",
                    str(DOCTOR_SCRIPT),
                    "--offline",
                    "--receipt",
                    str(receipt_path),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            self.assertIn(proc.returncode, {0, 1}, proc.stderr)
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["exit_code"], proc.returncode)
            self.assertEqual(payload["stages"]["security"]["status"], "passed")
            self.assertEqual(payload["stages"]["reliability"]["status"], "passed")
            self.assertEqual(
                payload["stages"]["openclaw_probe"]["status"], "skipped_offline"
            )
            self.assertIn(payload["stages"]["readiness"]["grade"], {"A", "B", "C"})
            self.assertRegex(payload["receipt_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertIn("Machine handoff receipt:", proc.stdout)
            self.assertNotIn("openclaw models status --probe", proc.stdout)

    def test_receipt_requires_offline_mode(self) -> None:
        proc = subprocess.run(
            ["bash", str(DOCTOR_SCRIPT), "--receipt", "/tmp/not-written.json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--receipt is offline-only", proc.stderr)


if __name__ == "__main__":
    unittest.main()
