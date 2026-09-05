"""Offline recovery uses exact scripts with synthetic paths and trap commands only."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


class TestOfflineCapabilityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cap = _module("andrea_capabilities")
        cls.grade = _module("andrea_readiness_grade")

    def test_explicit_offline_never_calls_external_capability_probes(self) -> None:
        with mock.patch.object(self.cap, "_read_dotenv_keys", return_value={}), \
             mock.patch.object(self.cap, "_which", return_value=True), \
             mock.patch.object(self.cap, "_cursor_diagnose_summary", return_value=("ready", "fixture")), \
             mock.patch.object(self.cap, "_openclaw_skills", side_effect=AssertionError("live OpenClaw")), \
             mock.patch.object(self.cap, "_gh_auth_state", side_effect=AssertionError("live GitHub")), \
             mock.patch.dict(os.environ, {"CURSOR_API_KEY": "offline-fixture"}, clear=True):
            rows = {row.id: row.as_dict() for row in self.cap.build_matrix(offline=True)}
        for key in ("openclaw:skills_list", "github:auth", "skill:acp-router", "acp_tool:acpx"):
            self.assertEqual(rows[key]["status"], "not_run")
            self.assertIn("Not verified", rows[key]["notes"])
        self.assertFalse(any(key.startswith("skill:") and key != "skill:acp-router" for key in rows))
        payload = {"ok": True, "rows": list(rows.values()), "summary": {"blocked": 0}}
        grade, reasons = self.grade.grade_from_payload(payload)
        self.assertEqual(grade, "C")
        self.assertEqual(set(reasons), {
            "critical_not_verified:openclaw:skills_list", "critical_not_verified:github:auth",
        })
        plan = self.grade.build_readiness_plan(payload, grade)
        self.assertFalse(plan["safe_for_autonomous_ops"])
        self.assertEqual(plan["who_acts_first"], "owner")
        self.assertEqual(plan["blocker_count"], 2)
        self.assertEqual({action["status"] for action in plan["actions"]}, {"not_run"})
        self.assertIn("was not verified in offline mode", plan["next_action"])
        self.assertIn("owner must approve a separate live readiness check", plan["next_action"])

    def test_default_matrix_still_uses_existing_external_probes(self) -> None:
        with mock.patch.object(self.cap, "_read_dotenv_keys", return_value={}), \
             mock.patch.object(self.cap, "_which", return_value=True), \
             mock.patch.object(self.cap, "_cursor_diagnose_summary", return_value=("ready", "fixture")), \
             mock.patch.object(self.cap, "_openclaw_skills", return_value=("ready", "", "")) as skills, \
             mock.patch.object(self.cap, "_gh_auth_state", return_value=("ready", "fixture")) as auth, \
             mock.patch.dict(os.environ, {"CURSOR_API_KEY": "offline-fixture"}, clear=True):
            rows = {row.id: row.status for row in self.cap.build_matrix()}
        skills.assert_called_once_with()
        auth.assert_called_once_with({}, {})
        self.assertEqual(rows["openclaw:skills_list"], "ready")
        self.assertEqual(rows["github:auth"], "ready")

    def test_readiness_forwards_only_explicit_offline(self) -> None:
        result = subprocess.CompletedProcess([], 0, '{"ok":true,"rows":[]}', "")
        with mock.patch.object(self.grade.subprocess, "run", return_value=result) as run:
            self.grade.run_capabilities()
            self.assertNotIn("--offline", run.call_args.args[0])
            self.grade.run_capabilities(offline=True)
            self.assertEqual(run.call_args.args[0][-1], "--offline")


class TestOfflineDoctorRecovery(unittest.TestCase):
    """Never uses the real host's env files, runtime, security scans, or providers."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="andrea-offline-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in (
            "andrea_doctor.sh", "andrea_capabilities.py", "andrea_readiness_grade.py",
            "andrea_reliability_probes.sh", "andrea_doctor_receipt.py", "env_loader.py",
        ):
            shutil.copyfile(SCRIPTS / name, self.scripts / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.log = self.root / "forbidden-calls.log"
        for name in ("gh", "openclaw", "curl", "acpx"):
            trap = self.bin / name
            trap.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$ANDREA_TEST_CALL_LOG"\nexit 97\n',
                encoding="utf-8",
            )
            trap.chmod(0o755)
        # Explicit Python path mocking is confined to this synthetic child tree.
        # HOME and CODEX_HOME are not reassigned or used as writable fixture roots.
        hooks = self.root / "python-fixtures"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "Path.home = classmethod(lambda cls: Path(os.environ['ANDREA_TEST_USER_DIR']))\n",
            encoding="utf-8",
        )
        self.user_dir = self.root / "synthetic-user"
        self.user_dir.mkdir()
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "PYTHONPATH": str(hooks),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.root),
            "ANDREA_TEST_USER_DIR": str(self.user_dir),
            "ANDREA_TEST_CALL_LOG": str(self.log),
            "CURSOR_API_KEY": "offline-fixture-not-a-real-key",
            "OPENCLAW_ENFORCE": "1",
            "MODEL_GUARD_ON_FAIL": "1",
            "ANDREA_SYNC_DOCTOR": "1",
            "ANDREA_SYNC_REQUIRED": "1",
            "RUN_LIVE_PROBES": "1",
        }
        (self.scripts / "andrea_security_sanity.sh").write_text(
            '#!/bin/sh\nprintf "%s\\n" "synthetic security stage passed"\n', encoding="utf-8",
        )
        (self.scripts / "cursor_openclaw.py").write_text(
            'import json\nprint(json.dumps({"ok": True, "api_key_present": False}))\n',
            encoding="utf-8",
        )
        for name in ("andrea_openclaw_enforce.sh", "andrea_model_guard.sh"):
            (self.scripts / name).write_text(
                '#!/bin/sh\nprintf "%s\\n" "$0" >> "$ANDREA_TEST_CALL_LOG"\nexit 97\n',
                encoding="utf-8",
            )
        (self.scripts / "andrea_sync_health.py").write_text(
            'import os\nfrom pathlib import Path\n'
            'with Path(os.environ["ANDREA_TEST_CALL_LOG"]).open("a") as out:\n'
            '    out.write("live health invoked\\n")\nraise SystemExit(97)\n',
            encoding="utf-8",
        )

    def run_script(self, name: str, *args: str, **env_overrides: str) -> subprocess.CompletedProcess:
        launcher = "/bin/bash" if name.endswith(".sh") else sys.executable
        return subprocess.run(
            [launcher, str(self.scripts / name), *args], cwd=self.root,
            env={**self.env, **env_overrides}, capture_output=True, text=True,
            timeout=30, check=False,
        )

    def assert_no_live_calls(self) -> None:
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")

    def test_exact_offline_doctor_overrides_all_inherited_live_options(self) -> None:
        receipt = self.root / "data" / "andrea-doctor-receipt.json"
        result = self.run_script("andrea_doctor.sh", "--offline", "--receipt", str(receipt))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_no_live_calls()
        self.assertIn("Offline doctor complete.", result.stdout, result.stdout + result.stderr)
        self.assertGreaterEqual(result.stdout.count("--- Operator next steps ---"), 2)
        self.assertIn("not verified in offline mode", result.stdout)
        self.assertNotIn(">>> [3.5/4]", result.stdout)
        self.assertNotIn(">>> [3.6/4]", result.stdout)
        payload = json.loads(receipt.read_text())
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["overall_status"], "blocked")
        self.assertEqual(payload["blocked_reason"], "grade_c")
        self.assertEqual(payload["failed_stages"], [])
        self.assertEqual(payload["stages"]["openclaw_probe"]["status"], "skipped_offline")
        self.assertEqual(payload["stages"]["security"]["status"], "passed")
        self.assertEqual(payload["stages"]["reliability"]["status"], "passed")
        self.assertFalse(payload["handoff"]["safe_for_autonomous_ops"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.env["CURSOR_API_KEY"], receipt.read_text())
        consumed = self.run_script(
            "andrea_doctor_receipt.py", "--consume", str(receipt), "--audience", "dashboard",
        )
        self.assertEqual(consumed.returncode, 0, consumed.stderr)
        packet = json.loads(consumed.stdout)
        self.assertTrue(packet["trusted_receipt"])
        self.assertTrue(packet["may_continue_offline_code"])
        self.assertFalse(packet["safe_for_autonomous_ops"])

    def test_capabilities_cli_offline_strict_is_unverified_not_missing(self) -> None:
        result = self.run_script("andrea_capabilities.py", "--offline", "--strict", "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assert_no_live_calls()
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        rows = {row["id"]: row for row in payload["rows"]}
        for key in ("openclaw:skills_list", "github:auth"):
            self.assertEqual(rows[key]["status"], "not_run")
        self.assertEqual(payload["summary"]["not_run"], 4)

    def test_reliability_cli_offline_ignores_inherited_live_probes(self) -> None:
        result = self.run_script("andrea_reliability_probes.sh", "--offline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_live_calls()
        self.assertNotIn("-------- LIVE:", result.stdout)

    def test_skip_model_probe_alone_cannot_mint_offline_receipt(self) -> None:
        receipt = self.root / "should-not-exist.json"
        result = self.run_script(
            "andrea_doctor.sh", "--receipt", str(receipt), SKIP_OPENCLAW_PROBE="1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--receipt is offline-only", result.stderr)
        self.assertFalse(receipt.exists())
        self.assert_no_live_calls()

    def test_legacy_skip_model_probe_preserves_other_owner_invoked_paths(self) -> None:
        (self.scripts / "andrea_readiness_grade.py").write_text(
            'print("--- Operator next steps ---\\nWho acts first: fixture\\n'
            '--- End operator next steps ---")\n', encoding="utf-8",
        )
        (self.scripts / "andrea_reliability_probes.sh").write_text("exit 0\n", encoding="utf-8")
        result = self.run_script("andrea_doctor.sh", SKIP_OPENCLAW_PROBE="1")
        self.assertEqual(result.returncode, 1)
        calls = self.log.read_text()
        self.assertIn("andrea_openclaw_enforce.sh", calls)
        self.assertIn("live health invoked", calls)
        self.assertNotIn("models status", calls)

    def test_explicit_offline_without_receipt_also_skips_external_work(self) -> None:
        result = self.run_script("andrea_doctor.sh", "--offline")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Offline doctor complete.", result.stdout)
        self.assert_no_live_calls()

    def test_default_doctor_still_attempts_owner_invoked_model_probe(self) -> None:
        (self.scripts / "andrea_readiness_grade.py").write_text(
            'print("--- Operator next steps ---\\nWho acts first: fixture\\n'
            '--- End operator next steps ---")\n', encoding="utf-8",
        )
        (self.scripts / "andrea_reliability_probes.sh").write_text("exit 0\n", encoding="utf-8")
        result = self.run_script(
            "andrea_doctor.sh", OPENCLAW_ENFORCE="0", ANDREA_SYNC_DOCTOR="0",
            MODEL_GUARD_ON_FAIL="0",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("openclaw models status --probe", self.log.read_text())
        self.assertNotIn("Offline doctor complete.", result.stdout)


if __name__ == "__main__":
    unittest.main()
