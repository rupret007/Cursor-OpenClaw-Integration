"""Tests for the offline doctor's redaction-safe machine handoff receipt."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPT_SCRIPT = REPO_ROOT / "scripts" / "andrea_doctor_receipt.py"
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "andrea_doctor.sh"
SERVER_PY = REPO_ROOT / "services" / "andrea_sync" / "server.py"
SERVER_PY_BLOB = "8c5efa82c51534d93503b9cb655ba3eeefe2d39c"
OUTBOUND_CONFIRM_PATTERN = r"^\s*(?:send it|send it now|send now)\s*[.!]?\s*$"


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


def _schema1_leftover(mod, *, security: str = "failed") -> dict:
    """Reproduce a leftover #18 receipt that blocked overall_status but kept Grade A copy."""
    receipt = {
        "schema_version": 1,
        "kind": mod.KIND,
        "mode": "offline",
        "overall_status": "blocked",
        "exit_code": 1,
        "stages": {
            "security": {"status": security},
            "readiness": {"status": "ready", "grade": "A"},
            "reliability": {"status": "not_run"},
            "openclaw_probe": {"status": "skipped_offline"},
        },
        "handoff": {
            "safe_for_autonomous_ops": False,
            "blocker_count": 0,
            "who_acts_first": "coding_agent",
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
        "commands": {"rerun": mod.RERUN_COMMAND},
    }
    receipt["receipt_fingerprint"] = mod.compute_fingerprint(receipt)
    return receipt


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
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["overall_status"], "ready")
        self.assertEqual(first["blocked_reason"], "")
        self.assertEqual(first["failed_stages"], [])
        self.assertTrue(first["handoff"]["safe_for_autonomous_ops"])
        self.assertEqual(first["handoff"]["who_acts_first"], "coding_agent")
        self.assertEqual(first["handoff"]["next_action"], "Continue the assigned offline test.")
        self.assertEqual(first["receipt_fingerprint"], second["receipt_fingerprint"])
        self.assertRegex(first["receipt_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["receipt_fingerprint"], self._mod.compute_fingerprint(first))
        serialized = json.dumps(first)
        self.assertNotIn("raw-secret-marker", serialized)
        self.assertNotIn("private/path-that-must-not-be-copied", serialized)
        self.assertNotIn("probe_notes", serialized)
        self.assertIn("--offline --receipt", first["commands"]["rerun"])
        self.assertIn("--verify", first["commands"]["verify"])
        self.assertIn("--consume", first["commands"]["consume"])
        self.assertIn("coding_agent", first["commands"]["consume"])

    def test_failed_stage_overrides_grade_a_next_steps(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="not_run",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertEqual(receipt["blocked_reason"], "stage_failed:security,reliability")
        self.assertEqual(receipt["failed_stages"], ["security", "reliability"])
        self.assertFalse(receipt["handoff"]["safe_for_autonomous_ops"])
        self.assertEqual(receipt["handoff"]["who_acts_first"], "owner")
        self.assertEqual(receipt["stages"]["security"]["status"], "failed")
        self.assertIn("Doctor stage failed (security, reliability)", receipt["handoff"]["next_action"])
        self.assertNotIn("Continue the assigned offline test.", receipt["handoff"]["next_action"])
        self.assertIn("Do not treat this host as ready", receipt["handoff"]["coding_agent_next_action"])
        self.assertIn("Keep outbound drafts", receipt["handoff"]["andrea_next_action"])
        self.assertEqual(receipt["handoff"]["actions"][0]["id"], "doctor:security")
        ok, reason, verified = self._mod.verify_receipt(receipt)
        self.assertTrue(ok, reason)
        self.assertEqual(verified["receipt_fingerprint"], receipt["receipt_fingerprint"])

    def test_reliability_failure_blocks_and_names_the_stage(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("B"),
            security_status="passed",
            reliability_status="failed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertEqual(receipt["blocked_reason"], "stage_failed:reliability")
        self.assertEqual(receipt["failed_stages"], ["reliability"])
        self.assertEqual(receipt["handoff"]["who_acts_first"], "owner")
        self.assertIn("reliability", receipt["handoff"]["next_action"])

    def test_grade_c_with_passing_stages_keeps_capability_next_action(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("C"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertEqual(receipt["blocked_reason"], "grade_c")
        self.assertEqual(receipt["failed_stages"], [])
        self.assertEqual(receipt["handoff"]["who_acts_first"], "owner")
        self.assertEqual(receipt["handoff"]["next_action"], "Continue the assigned offline test.")
        packet = self._mod.audience_packet(receipt, "bob")
        self.assertTrue(packet["trusted_receipt"])
        self.assertTrue(packet["may_continue_offline_code"])
        self.assertTrue(packet["must_wait_for_owner"])
        self.assertEqual(packet["next_action"], "Run offline verification.")

    def test_missing_readiness_fails_closed_to_owner(self) -> None:
        receipt = self._mod.build_receipt(
            {},
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        self.assertEqual(receipt["overall_status"], "blocked")
        self.assertEqual(receipt["blocked_reason"], "grade_c")
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
            self.assertEqual(payload["blocked_reason"], "")
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_consume_aliases_share_coding_agent_packet(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        packets = {
            name: self._mod.audience_packet(receipt, name)
            for name in ("bob", "codex", "grok", "claude", "coding_agent")
        }
        for name, packet in packets.items():
            with self.subTest(audience=name):
                self.assertEqual(packet["audience"], "coding_agent")
                self.assertTrue(packet["trusted_receipt"])
                self.assertTrue(packet["may_continue_offline_code"])
                self.assertFalse(packet["must_wait_for_owner"])
                self.assertEqual(packet["next_action"], "Run offline verification.")
                self.assertNotIn("raw-secret-marker", json.dumps(packet))
        self.assertEqual(
            {packets[name]["next_action"] for name in packets},
            {"Run offline verification."},
        )
        dashboard = self._mod.audience_packet(receipt, "dashboard")
        self.assertEqual(dashboard["audience"], "dashboard")
        self.assertEqual(dashboard["next_action"], "Continue the assigned offline test.")
        andrea = self._mod.audience_packet(receipt, "andrea")
        self.assertEqual(andrea["next_action"], "Keep the draft pending.")
        self.assertIn("Keep Private API off.", andrea["holds"])

    def test_failed_stage_consume_blocks_every_audience(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        for name in ("bob", "codex", "grok", "claude", "andrea", "owner", "dashboard"):
            packet = self._mod.audience_packet(receipt, name)
            with self.subTest(audience=name):
                self.assertEqual(packet["overall_status"], "blocked")
                self.assertEqual(packet["blocked_reason"], "stage_failed:security")
                self.assertFalse(packet["safe_for_autonomous_ops"])
                self.assertFalse(packet["may_continue_offline_code"])
                self.assertTrue(packet["must_wait_for_owner"])
                self.assertNotIn("Continue the assigned offline test.", packet["next_action"])

    def test_leftover_schema1_receipt_is_overridden_on_consume(self) -> None:
        leftover = _schema1_leftover(self._mod)
        ok, reason, verified = self._mod.verify_receipt(leftover)
        self.assertTrue(ok, reason)
        self.assertEqual(verified["handoff"]["next_action"], "Continue the assigned offline test.")
        packet = self._mod.audience_packet(leftover, "codex")
        self.assertTrue(packet["trusted_receipt"])
        self.assertEqual(packet["overall_status"], "blocked")
        self.assertEqual(packet["blocked_reason"], "stage_failed:security,reliability")
        self.assertFalse(packet["may_continue_offline_code"])
        self.assertIn("Do not treat this host as ready", packet["next_action"])
        self.assertNotIn("Continue the assigned offline test.", packet["next_action"])
        self.assertNotIn("Run offline verification.", packet["next_action"])

    def test_fingerprint_mismatch_fails_closed(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        receipt["handoff"]["next_action"] = "secret-smuggle"
        ok, reason, verified = self._mod.verify_receipt(receipt)
        self.assertFalse(ok)
        self.assertEqual(reason, "fingerprint_mismatch")
        self.assertEqual(verified, {})
        packet = self._mod.audience_packet(receipt, "bob", trusted=False, reason=reason)
        self.assertFalse(packet["trusted_receipt"])
        self.assertEqual(packet["blocked_reason"], "invalid_receipt")
        self.assertFalse(packet["may_continue_offline_code"])
        self.assertNotIn("secret-smuggle", json.dumps(packet))

    def test_tampered_ready_status_fails_closed_even_with_new_fingerprint(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        receipt["overall_status"] = "ready"
        receipt["receipt_fingerprint"] = self._mod.compute_fingerprint(receipt)
        ok, reason, _verified = self._mod.verify_receipt(receipt)
        self.assertFalse(ok)
        self.assertEqual(reason, "overall_status_mismatch")

    def test_unexpected_probe_notes_are_rejected(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        receipt["probe_notes"] = "raw-secret-marker"
        receipt["receipt_fingerprint"] = self._mod.compute_fingerprint(receipt)
        ok, reason, _verified = self._mod.verify_receipt(receipt)
        self.assertFalse(ok)
        self.assertEqual(reason, "unexpected_keys")

    def test_unknown_audience_fails_closed_to_owner(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        packet = self._mod.audience_packet(receipt, "unknown-lane")
        self.assertFalse(packet["trusted_receipt"])
        self.assertEqual(packet["audience"], "owner")
        self.assertEqual(packet["reason"], "invalid_audience")
        self.assertFalse(packet["may_continue_offline_code"])

    def test_cli_verify_and_consume_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = root / "readiness.json"
            receipt_path = root / "receipt.json"
            readiness_path.write_text(json.dumps(_readiness("A")), encoding="utf-8")
            write = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--readiness-json",
                    str(readiness_path),
                    "--security-status",
                    "failed",
                    "--reliability-status",
                    "passed",
                    "--openclaw-status",
                    "skipped_offline",
                    "--exit-code",
                    "1",
                    "--output",
                    str(receipt_path),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(write.returncode, 0, write.stderr)
            verify = subprocess.run(
                [sys.executable, str(RECEIPT_SCRIPT), "--verify", str(receipt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            verify_payload = json.loads(verify.stdout)
            self.assertTrue(verify_payload["ok"])
            self.assertEqual(verify_payload["overall_status"], "blocked")
            self.assertEqual(verify_payload["blocked_reason"], "stage_failed:security")
            consume = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--consume",
                    str(receipt_path),
                    "--audience",
                    "grok",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(consume.returncode, 0, consume.stderr)
            packet = json.loads(consume.stdout)
            self.assertEqual(packet["audience"], "coding_agent")
            self.assertEqual(packet["audience_requested"], "grok")
            self.assertFalse(packet["may_continue_offline_code"])
            self.assertIn("Do not treat this host as ready", packet["next_action"])
            self.assertIn("Restore the failed doctor stage (security)", packet["owner_next_action"])
            summary = subprocess.run(
                [sys.executable, str(RECEIPT_SCRIPT), "--summary", str(receipt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assertIn("Receipt verify: valid", summary.stdout)
            self.assertIn("overall_status=blocked", summary.stdout)
            self.assertIn("blocked_reason=stage_failed:security", summary.stdout)
            self.assertIn("who_acts_first=owner", summary.stdout)
            missing = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--consume",
                    str(root / "missing.json"),
                    "--audience",
                    "bob",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(missing.returncode, 1)
            missing_packet = json.loads(missing.stdout)
            self.assertFalse(missing_packet["trusted_receipt"])
            self.assertEqual(missing_packet["reason"], "missing_file")

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
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["exit_code"], proc.returncode)
            self.assertEqual(payload["stages"]["security"]["status"], "passed")
            self.assertEqual(payload["stages"]["reliability"]["status"], "passed")
            self.assertEqual(
                payload["stages"]["openclaw_probe"]["status"], "skipped_offline"
            )
            self.assertIn(payload["stages"]["readiness"]["grade"], {"A", "B", "C"})
            self.assertRegex(payload["receipt_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertIn(payload["blocked_reason"], {"", "grade_c", "nonzero_exit"})
            self.assertEqual(payload["failed_stages"], [])
            if proc.returncode == 1:
                self.assertEqual(payload["overall_status"], "blocked")
                self.assertFalse(payload["handoff"]["safe_for_autonomous_ops"])
            self.assertIn("Machine handoff receipt:", proc.stdout)
            self.assertIn("Receipt verify: valid", proc.stdout)
            self.assertIn("overall_status=", proc.stdout)
            self.assertIn("Consume:", proc.stdout)
            self.assertNotIn("openclaw models status --probe", proc.stdout)
            consume = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--consume",
                    str(receipt_path),
                    "--audience",
                    "dashboard",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(consume.returncode, 0, consume.stderr)
            packet = json.loads(consume.stdout)
            self.assertTrue(packet["trusted_receipt"])
            self.assertEqual(packet["overall_status"], payload["overall_status"])
            self.assertIn("Keep Private API off.", packet["holds"])
            serialized = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("raw-secret-marker", serialized)
            self.assertNotIn("probe_notes", serialized)

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

    def test_consume_withdraws_current_authority_from_stale_ready_receipt(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            self._mod.write_receipt(path, receipt)
            original_bytes = path.read_bytes()
            current_rc, current = self._mod.consume_receipt(
                path, "grok", now=path.stat().st_mtime + 90.0
            )
            self.assertEqual(current_rc, 0)
            self.assertEqual(current["receipt_state"], "current")
            self.assertTrue(current["trusted_receipt"])
            self.assertTrue(current["safe_for_autonomous_ops"])
            self.assertTrue(current["fresh"])
            stale_rc, stale = self._mod.consume_receipt(
                path,
                "grok",
                now=path.stat().st_mtime + self._mod.RECEIPT_MAX_AGE_SECONDS + 1,
            )
            self.assertEqual(stale_rc, 1)
            self.assertEqual(stale["receipt_state"], "stale")
            self.assertTrue(stale["receipt_verified"])
            self.assertFalse(stale["trusted_receipt"])
            self.assertFalse(stale["safe_for_autonomous_ops"])
            self.assertTrue(stale["may_continue_offline_code"])
            self.assertFalse(stale["must_wait_for_owner"])
            self.assertEqual(stale["who_acts_first"], "coding_agent")
            self.assertEqual(stale["blocked_reason"], "stale_receipt")
            self.assertEqual(stale["last_verified"]["grade"], "A")
            self.assertEqual(stale["last_verified"]["overall_status"], "ready")
            self.assertIn("older than 24 hours", stale["next_action"])
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertNotIn("receipt_path", stale)
            self.assertNotIn(str(path), json.dumps(stale))

    def test_stale_failed_stage_keeps_owner_hold_for_every_consumer(self) -> None:
        receipt = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            self._mod.write_receipt(path, receipt)
            now = path.stat().st_mtime + self._mod.RECEIPT_MAX_AGE_SECONDS + 5
            for audience in ("bob", "codex", "grok", "claude", "andrea", "owner", "dashboard"):
                rc, packet = self._mod.consume_receipt(path, audience, now=now)
                with self.subTest(audience=audience):
                    self.assertEqual(rc, 1)
                    self.assertEqual(packet["receipt_state"], "stale")
                    self.assertTrue(packet["must_wait_for_owner"])
                    self.assertFalse(packet["may_continue_offline_code"])
                    self.assertEqual(packet["last_verified"]["failed_stages"], ["security"])
                    self.assertIn("clearance has not been verified", packet["next_action"])

    def test_cli_verify_and_summary_fail_closed_on_stale_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = root / "readiness.json"
            receipt_path = root / "receipt.json"
            readiness_path.write_text(json.dumps(_readiness("A")), encoding="utf-8")
            write = subprocess.run(
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
            self.assertEqual(write.returncode, 0, write.stderr)
            old = receipt_path.stat().st_mtime - (self._mod.RECEIPT_MAX_AGE_SECONDS + 10)
            os.utime(receipt_path, (old, old))
            verify = subprocess.run(
                [sys.executable, str(RECEIPT_SCRIPT), "--verify", str(receipt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(verify.returncode, 1, verify.stderr)
            verify_payload = json.loads(verify.stdout)
            self.assertFalse(verify_payload["ok"])
            self.assertEqual(verify_payload["reason"], "receipt_too_old")
            self.assertTrue(verify_payload["receipt_verified"])
            self.assertEqual(verify_payload["overall_status"], "blocked")
            consume = subprocess.run(
                [
                    sys.executable,
                    str(RECEIPT_SCRIPT),
                    "--consume",
                    str(receipt_path),
                    "--audience",
                    "bob",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(consume.returncode, 1, consume.stderr)
            packet = json.loads(consume.stdout)
            self.assertEqual(packet["receipt_state"], "stale")
            self.assertFalse(packet["safe_for_autonomous_ops"])
            summary = subprocess.run(
                [sys.executable, str(RECEIPT_SCRIPT), "--summary", str(receipt_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(summary.returncode, 1, summary.stderr)
            self.assertIn("receipt_state=stale", summary.stdout)
            self.assertIn("overall_status=blocked", summary.stdout)
            self.assertIn("blocked_reason=stale_receipt", summary.stdout)

    def test_handoff_consult_gates_live_api_but_not_absent_evidence(self) -> None:
        ready = self._mod.build_receipt(
            _readiness("A"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        grade_c = self._mod.build_receipt(
            _readiness("C"),
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        failed = self._mod.build_receipt(
            _readiness("A"),
            security_status="failed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ready_path = root / "ready.json"
            grade_c_path = root / "grade-c.json"
            failed_path = root / "failed.json"
            data_root = root / "checkout"
            canonical = data_root / "data" / "andrea-doctor-receipt.json"
            self._mod.write_receipt(ready_path, ready)
            self._mod.write_receipt(grade_c_path, grade_c)
            self._mod.write_receipt(failed_path, failed)
            self._mod.write_receipt(canonical, ready)
            absent = self._mod.consult_receipt_for_handoff(None, source="absent")
            self.assertFalse(absent["consulted"])
            self.assertIsNone(self._mod.live_handoff_block_reason(absent, "api"))
            current = self._mod.consult_receipt_for_handoff(
                ready_path, source="explicit", now=ready_path.stat().st_mtime + 3
            )
            self.assertTrue(current["consulted"])
            self.assertTrue(current["safe_for_autonomous_ops"])
            self.assertIsNone(self._mod.live_handoff_block_reason(current, "api"))
            stale = self._mod.consult_receipt_for_handoff(
                ready_path,
                source="explicit",
                now=ready_path.stat().st_mtime + self._mod.RECEIPT_MAX_AGE_SECONDS + 1,
            )
            self.assertEqual(stale["receipt_state"], "stale")
            self.assertIn("current authority", self._mod.live_handoff_block_reason(stale, "api") or "")
            self.assertIsNone(self._mod.live_handoff_block_reason(stale, "cli"))
            limited = self._mod.consult_receipt_for_handoff(
                grade_c_path, source="explicit", now=grade_c_path.stat().st_mtime + 3
            )
            self.assertTrue(limited["may_continue_offline_code"])
            self.assertIsNotNone(self._mod.live_handoff_block_reason(limited, "api"))
            self.assertIsNone(self._mod.live_handoff_block_reason(limited, "cli"))
            owner_hold = self._mod.consult_receipt_for_handoff(
                failed_path, source="explicit", now=failed_path.stat().st_mtime + 3
            )
            self.assertIsNotNone(self._mod.live_handoff_block_reason(owner_hold, "api"))
            self.assertIsNotNone(self._mod.live_handoff_block_reason(owner_hold, "cli"))
            path, source = self._mod.resolve_handoff_receipt_path(
                "", local_repo=data_root, cwd=root, environ={}
            )
            self.assertEqual(source, "discovered")
            self.assertEqual(path, canonical)
            ignored_tmp, tmp_source = self._mod.resolve_handoff_receipt_path(
                "", local_repo=root, cwd=root, environ={}
            )
            self.assertEqual(tmp_source, "absent")
            self.assertIsNone(ignored_tmp)
            explicit_tmp = root / "tmp-not-canonical.json"
            self._mod.write_receipt(explicit_tmp, ready)
            forced, forced_source = self._mod.resolve_handoff_receipt_path(
                str(explicit_tmp), local_repo=root, cwd=root, environ={}
            )
            self.assertEqual(forced_source, "explicit")
            self.assertEqual(forced, explicit_tmp)

    def test_receipt_help_names_consume_path(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(RECEIPT_SCRIPT), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--consume", proc.stdout)
        self.assertIn("--verify", proc.stdout)
        self.assertIn("--audience", proc.stdout)
        self.assertIn("bob", proc.stdout)

    def test_server_py_blob_stays_on_fence(self) -> None:
        payload = SERVER_PY.read_bytes()
        digest = hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()
        self.assertEqual(digest, SERVER_PY_BLOB)
        from services.andrea_sync.server import OUTBOUND_CONFIRM_RE

        self.assertEqual(OUTBOUND_CONFIRM_RE.pattern, OUTBOUND_CONFIRM_PATTERN)
        self.assertIsNotNone(OUTBOUND_CONFIRM_RE.fullmatch("send it"))
        self.assertIsNotNone(OUTBOUND_CONFIRM_RE.fullmatch("send it now"))
        self.assertIsNotNone(OUTBOUND_CONFIRM_RE.fullmatch("send now"))
        self.assertIsNone(OUTBOUND_CONFIRM_RE.fullmatch("send it please"))
        self.assertIsNone(OUTBOUND_CONFIRM_RE.fullmatch("yes"))


if __name__ == "__main__":
    unittest.main()
