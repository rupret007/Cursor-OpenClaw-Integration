"""Check Cursor backend selection, plan gating, and subprocess boundaries."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDOFF = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"
WRAPPER = HANDOFF.with_name("cursor_cli_fallback.sh")
ACCOUNT = "a" * 64


class CursorHandoffModelFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.local = Path(temporary.name)
        self.home = self.local / "operator-home"
        self.home.mkdir(mode=0o700)
        self.fake = self.local / "agent"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:-1], 'stdin': sys.stdin.read(),\n"
            "                  'environ': sorted(os.environ), 'home': os.environ.get('HOME')}))\n",
            encoding="utf-8",
        )
        self.fake.chmod(0o700)
        self.receipt = self.local / "plan.json"
        self.env = {
            "PATH": os.defpath, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
            "CURSOR_CLI_BIN": str(self.fake), "CURSOR_PLAN_READING": str(self.receipt),
            "CURSOR_PLAN_ACCOUNT": ACCOUNT,
        }

    def write_receipt(self, spend: int = 400) -> None:
        now = int(time.time())
        self.receipt.touch(mode=0o600)
        self.receipt.write_text(json.dumps({
            "account_binding": ACCOUNT, "observed_at": now,
            "period_start": now - 3_600, "period_end": now + 3_600,
            "included_spend": spend, "included_limit": 1_000,
            "billing": dict.fromkeys(("has_credits", "unlimited", "on_demand_enabled",
                                       "usage_billing_enabled", "auto_top_up_enabled"), False),
        }), encoding="utf-8")

    def handoff(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(HANDOFF), "--repo", str(self.local), "--prompt", "Review fixture",
             "--json", "--model", "fixture-api-model", *args],
            cwd=self.local, env=self.env, capture_output=True, text=True, check=False, timeout=15,
        )
        return proc.returncode, json.loads(proc.stdout)

    def test_api_dry_run_preserves_explicit_model(self) -> None:
        self.env["CURSOR_API_KEY"] = "fixture-api-key"
        code, payload = self.handoff("--dry-run", "--mode", "api")
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["backend"], "api")
        self.assertEqual(payload["model"], "fixture-api-model")

    def test_cli_dry_run_reports_real_model_and_validates_the_local_plan(self) -> None:
        code, unread = self.handoff("--dry-run", "--mode", "cli")
        self.assertEqual(code, 0)
        self.assertFalse(unread["ok"])
        self.assertFalse(unread["cli_plan_eligible"])
        self.assertIn("readable plan", unread["cli_plan_error"])
        self.assertEqual(unread["model"], "composer-2.5")
        self.write_receipt(900)
        _, at_threshold = self.handoff("--dry-run", "--mode", "cli")
        self.assertFalse(at_threshold["ok"])
        self.assertFalse(at_threshold["cli_plan_eligible"])
        self.write_receipt(899)
        _, eligible = self.handoff("--dry-run", "--mode", "cli")
        self.assertTrue(eligible["ok"])
        self.assertTrue(eligible["cli_plan_eligible"])
        self.assertEqual(eligible["model"], "composer-2.5")

    def test_cli_backend_uses_headless_ask_and_only_eligible_operator_environment(self) -> None:
        self.write_receipt()
        self.env.update({
            "CURSOR_API_KEY": "fixture-api-key", "CURSOR_AUTH_TOKEN": "fixture-session-token",
            "OPENAI_API_KEY": "fixture-model-key", "CURSOR_BASE_URL": "ignored endpoint",
        })
        (self.local / ".env").write_text("XDG_CONFIG_HOME=untrusted-profile\n", encoding="utf-8")
        code, result = self.handoff("--mode", "auto")
        self.assertEqual(code, 0)
        self.assertEqual(result["backend"], "cli")
        out = json.loads(result["stdout"])
        self.assertEqual(out["argv"], ["--print", "--output-format", "text", "--model", "composer-2.5", "--mode=ask"])
        self.assertEqual(out["stdin"], "")
        self.assertEqual(out["home"], str(self.home))
        self.assertTrue(set(out["environ"]) <= {"HOME", "PATH", "LC_CTYPE"})
        self.assertEqual(result["model"], "composer-2.5")

    def test_project_dotenv_cannot_supply_subscription_receipt(self) -> None:
        self.write_receipt()
        del self.env["CURSOR_PLAN_READING"]
        del self.env["CURSOR_PLAN_ACCOUNT"]
        (self.local / ".env").write_text(
            f"CURSOR_PLAN_READING={self.receipt}\nCURSOR_PLAN_ACCOUNT={ACCOUNT}\n",
            encoding="utf-8",
        )
        code, payload = self.handoff("--mode", "cli")
        self.assertEqual(code, 5)
        self.assertFalse(payload["ok"])
        self.assertIn("readable plan", payload["stderr"])

    def test_cli_and_dry_run_diagnostics_do_not_call_cursor(self) -> None:
        spec = importlib.util.spec_from_file_location("cursor_handoff_diagnostic", HANDOFF)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
        for mode in ("cli", "auto", "api"):
            with self.subTest(mode=mode):
                argv = [str(HANDOFF), "--diagnose", "--mode", mode]
                if mode == "api":
                    argv.append("--dry-run")
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.dict(os.environ, self.env | {"CURSOR_API_KEY": "fixture-api-key"}, clear=True),
                    mock.patch.object(module, "emit_text"),
                    mock.patch.object(module.CursorApiClient, "get_endpoint", side_effect=AssertionError("live request"))
                    as get_endpoint,
                ):
                    self.assertEqual(module.main(), 0)
                    get_endpoint.assert_not_called()

    def test_cli_output_with_credentials_and_personal_fields_is_withheld(self) -> None:
        self.write_receipt()
        credential = "gh" + "p_" + "z" * 20
        address = ".".join(("10", "91", "7", "15"))
        contact = "cursor-fixture" + "@" + "synthetic-contact.invalid"
        self.fake.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            f"print({credential!r})\nprint({address + ' ' + contact!r}, file=sys.stderr)\n"
            "sys.exit(9)\n",
            encoding="utf-8",
        )
        code, result = self.handoff("--mode", "cli")
        self.assertEqual(code, 5)
        self.assertEqual(result["stdout"], "[redacted]")
        self.assertEqual(result["stderr"], "[redacted]")

        result = subprocess.run(
            [str(WRAPPER), str(self.local), "Review fixture", "true"],
            cwd=self.local, env=self.env, capture_output=True, text=True,
            check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout.strip(), "[redacted]")
        self.assertEqual(result.stderr.strip(), "[redacted]")

    def test_direct_wrapper_withholds_oversized_stream_and_preserves_exit(self) -> None:
        self.write_receipt()
        self.fake.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "print('x' * (512 * 1024))\nprint('Safe failure.', file=sys.stderr)\nsys.exit(8)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(WRAPPER), str(self.local), "Review fixture", "true"],
            cwd=self.local, env=self.env, capture_output=True, text=True,
            check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 8)
        self.assertEqual(result.stdout.strip(), "[redacted]")
        self.assertEqual(result.stderr.strip(), "Safe failure.")

    def test_direct_wrapper_refuses_relative_binary(self) -> None:
        self.write_receipt()
        self.env["CURSOR_CLI_BIN"] = "./agent"
        result = subprocess.run(
            [str(WRAPPER), str(self.local), "Review fixture", "true"],
            cwd=self.local, env=self.env, capture_output=True, text=True,
            check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("absolute executable path", result.stderr)
        self.assertFalse(result.stdout)

    def test_cli_diagnose_omits_cloud_ssl_metadata_from_project_dotenv(self) -> None:
        self.write_receipt()
        contact = "fixture-ssl" + "@" + "synthetic-contact.invalid"
        (self.local / ".env").write_text(f"SSL_CERT_FILE={contact}\n", encoding="utf-8")
        code, payload = self.handoff("--mode", "cli", "--diagnose")
        self.assertEqual(code, 0)
        self.assertNotIn("ssl", payload["checks"])
        self.assertNotIn(contact, json.dumps(payload))

    def test_cli_dry_run_checks_a_credential_before_truncating_the_prompt(self) -> None:
        self.write_receipt()
        credential = "gh" + "p_" + "z" * 20
        formatted_prefix = "READ-ONLY MODE: Analyze/review/plan only. Do NOT edit files, commit, or open PRs.\n\nTask:\n"
        prompt = "x" * (500 - len(formatted_prefix) - 9) + " " + credential
        code, payload = self.handoff("--mode", "cli", "--dry-run", "--prompt", prompt)
        self.assertEqual(code, 0)
        self.assertEqual(payload["prompt_preview"], "[redacted]")

    def test_cli_reads_project_receipt_data_without_loading_project_code(self) -> None:
        self.write_receipt()
        scripts = self.local / "scripts"
        scripts.mkdir()
        (scripts / "andrea_doctor_receipt.py").write_text(
            "raise AssertionError('Unexpected validator import')\n", encoding="utf-8"
        )
        data = self.local / "data"
        data.mkdir()
        (data / "andrea-doctor-receipt.json").write_text("{}", encoding="utf-8")
        code, payload = self.handoff("--mode", "cli", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(payload["doctor_receipt"]["receipt_source"], "discovered")
        self.assertEqual(payload["doctor_receipt"]["receipt_state"], "invalid")
        self.assertTrue(payload["receipt_would_block"])

    def test_cli_receipt_projection_redacts_personal_action(self) -> None:
        self.write_receipt()
        script = REPO_ROOT / "scripts" / "andrea_doctor_receipt.py"
        spec = importlib.util.spec_from_file_location("fixture_doctor_receipt", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        contact = "fixture-account" + "@" + "synthetic-contact.invalid"
        document = module.build_receipt(
            {
                "grade": "A",
                "readiness_plan": {
                    "safe_for_autonomous_ops": True,
                    "blocker_count": 0,
                    "who_acts_first": "coding_agent",
                    "coding_agent_next_action": f"Review the receipt for {contact}",
                },
            },
            security_status="passed", reliability_status="passed",
            openclaw_status="skipped_offline", exit_code=0,
        )
        doctor = self.local / "doctor.json"
        module.write_receipt(doctor, document)
        for action in ("--diagnose", "--dry-run"):
            code, payload = self.handoff("--mode", "cli", action, "--receipt", str(doctor))
            self.assertEqual(code, 0)
            self.assertTrue(payload["doctor_receipt"]["receipt_verified"])
            self.assertEqual(payload["doctor_receipt"]["next_action"], "[redacted]")
            self.assertNotIn(contact, json.dumps(payload))

    def test_cli_rejects_oversized_or_nonregular_doctor_receipts(self) -> None:
        self.write_receipt()
        oversized = self.local / "large.json"
        oversized.write_bytes(b" " * (256 * 1024) + b"{}")
        receipts = [oversized]
        if hasattr(os, "mkfifo"):
            fifo = self.local / "doctor.pipe"
            os.mkfifo(fifo, mode=0o600)
            receipts.append(fifo)
        for receipt in receipts:
            with self.subTest(receipt=receipt.name):
                code, payload = self.handoff("--mode", "cli", "--dry-run", "--receipt", str(receipt))
                self.assertEqual(code, 0)
                self.assertEqual(payload["doctor_receipt"]["receipt_state"], "invalid")
                self.assertTrue(payload["receipt_would_block"])

    def test_direct_wrapper_requires_explicit_read_only_mode(self) -> None:
        self.write_receipt()
        result = subprocess.run(
            [str(WRAPPER), str(self.local), "Review fixture"],
            cwd=self.local, env=self.env, capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)
        self.assertIn("Read-only mode must be true or false.", result.stderr)

    def test_direct_wrapper_redacts_invalid_utf8_within_byte_cap(self) -> None:
        self.write_receipt()
        self.fake.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stdout.buffer.write(bytes([255]) * 250000)\nsys.exit(7)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(WRAPPER), str(self.local), "Review fixture", "true"],
            cwd=self.local, env=self.env, capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "[redacted]")
