import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_handoff.py"
)
SPEC = importlib.util.spec_from_file_location("cursor_handoff", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["cursor_handoff"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class CursorHandoffTests(unittest.TestCase):
    def test_parse_bool_text(self):
        self.assertTrue(MODULE.parse_bool_text("true"))
        self.assertTrue(MODULE.parse_bool_text("YES"))
        self.assertFalse(MODULE.parse_bool_text("false"))
        with self.assertRaises(ValueError):
            MODULE.parse_bool_text("maybe")

    def test_build_handoff_prompt_branch_toggle(self):
        with_branch = MODULE.build_handoff_prompt(
            "Do a review", read_only=True, branch="feature/x", include_branch=True
        )
        without_branch = MODULE.build_handoff_prompt(
            "Do a review", read_only=True, branch="feature/x", include_branch=False
        )
        self.assertIn("Target branch: feature/x", with_branch)
        self.assertNotIn("Target branch: feature/x", without_branch)

    def test_normalize_base_url(self):
        self.assertTrue(MODULE.normalize_base_url("https://api.cursor.com/").startswith("https://"))
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("file:///etc/passwd")

    def test_normalize_repo_input(self):
        local, url, err = MODULE.normalize_repo_input("owner/repo")
        self.assertIsNone(local)
        self.assertEqual(url, "https://github.com/owner/repo")
        self.assertIsNone(err)

    def test_choose_backend(self):
        backend, err = MODULE.choose_backend(
            requested_mode="auto",
            has_api_creds=True,
            cli_wrapper_path=pathlib.Path("/tmp/missing-wrapper"),
            cli_binary=None,
        )
        self.assertEqual(backend, "api")
        self.assertIsNone(err)

    def test_ssl_hint(self):
        hint = MODULE.build_ssl_hint("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate")
        self.assertIsNotNone(hint)
        no_hint = MODULE.build_ssl_hint("some other error")
        self.assertIsNone(no_hint)

    def test_emit_text_diagnose(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            MODULE.emit_text(
                {
                    "ok": True,
                    "diagnose": True,
                    "checks": {
                        "api_key_set": False,
                        "api_base_url": "https://api.cursor.com",
                        "requested_mode": "auto",
                        "suggested_backend": "none",
                        "cli_binary": None,
                    },
                }
            )
        out = buf.getvalue()
        self.assertIn("Diagnostics complete", out)
        self.assertNotIn("Handoff submitted successfully", out)

    def test_emit_text_dry_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            MODULE.emit_text(
                {
                    "ok": True,
                    "dry_run": True,
                    "backend": "api",
                    "backend_error": None,
                    "mode_requested": "api",
                    "read_only": True,
                    "branch": "b1",
                    "repo_input": "/tmp",
                }
            )
        out = buf.getvalue()
        self.assertIn("Dry run", out)
        self.assertNotIn("Handoff submitted successfully", out)

    def test_parse_args_two_way_op(self):
        original_argv = sys.argv[:]
        try:
            sys.argv = [
                "cursor_handoff.py",
                "--op",
                "conversation",
                "--agent-id",
                "bc-abc123",
                "--mode",
                "api",
                "--json",
            ]
            parsed = MODULE.parse_args()
            self.assertEqual(parsed.op, "conversation")
            self.assertEqual(parsed.agent_id, "bc-abc123")
            self.assertEqual(parsed.mode, "api")
            self.assertTrue(parsed.json)
        finally:
            sys.argv = original_argv

    def test_doctor_receipt_consult_and_live_gate(self):
        repo_root = pathlib.Path(__file__).resolve().parents[3]
        module = MODULE.load_doctor_receipt_module([repo_root])
        self.assertIsNotNone(module)
        ready = module.build_receipt(
            {
                "grade": "A",
                "readiness_plan": {
                    "safe_for_autonomous_ops": True,
                    "blocker_count": 0,
                    "who_acts_first": "coding_agent",
                    "next_action": "Continue the assigned offline test.",
                    "andrea_next_action": "Keep the draft pending.",
                    "coding_agent_next_action": "Run offline verification.",
                    "owner_next_action": "No owner setup is required.",
                    "holds": ["Do not send any live message."],
                    "routing": {
                        "andrea": "offline only",
                        "coding_agent": "offline code and tests only",
                        "owner": "owner-gated actions only",
                    },
                    "actions": [],
                },
            },
            security_status="passed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=0,
        )
        failed = module.build_receipt(
            {
                "grade": "A",
                "readiness_plan": {
                    "safe_for_autonomous_ops": False,
                    "blocker_count": 1,
                    "who_acts_first": "owner",
                    "next_action": "Stop.",
                    "andrea_next_action": "Keep the draft pending.",
                    "coding_agent_next_action": "Wait.",
                    "owner_next_action": "Restore security.",
                    "holds": ["Do not send any live message."],
                    "routing": {
                        "andrea": "offline only",
                        "coding_agent": "offline code and tests only",
                        "owner": "owner-gated actions only",
                    },
                    "actions": [],
                },
            },
            security_status="failed",
            reliability_status="passed",
            openclaw_status="skipped_offline",
            exit_code=1,
        )
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            ready_path = root / "ready.json"
            failed_path = root / "failed.json"
            module.write_receipt(ready_path, ready)
            module.write_receipt(failed_path, failed)
            absent = MODULE.consult_doctor_receipt(
                explicit="",
                local_repo=root,
                search_roots=[repo_root],
                environ={},
            )
            self.assertFalse(absent["consulted"])
            self.assertIsNone(MODULE.live_handoff_block_reason(absent, "api"))
            current = MODULE.consult_doctor_receipt(
                explicit=str(ready_path),
                local_repo=root,
                search_roots=[repo_root],
                now=ready_path.stat().st_mtime + 4,
                environ={},
            )
            self.assertTrue(current["safe_for_autonomous_ops"])
            self.assertIsNone(MODULE.live_handoff_block_reason(current, "api"))
            stale = MODULE.consult_doctor_receipt(
                explicit=str(ready_path),
                local_repo=root,
                search_roots=[repo_root],
                now=ready_path.stat().st_mtime + module.RECEIPT_MAX_AGE_SECONDS + 1,
                environ={},
            )
            self.assertEqual(stale["receipt_state"], "stale")
            self.assertIn("current authority", MODULE.live_handoff_block_reason(stale, "api") or "")
            owner_hold = MODULE.consult_doctor_receipt(
                explicit=str(failed_path),
                local_repo=root,
                search_roots=[repo_root],
                now=failed_path.stat().st_mtime + 4,
                environ={},
            )
            self.assertIsNotNone(MODULE.live_handoff_block_reason(owner_hold, "cli"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                MODULE.emit_text(
                    {
                        "ok": False,
                        "error": "blocked by receipt",
                        "doctor_receipt": stale,
                    }
                )
            rendered = buf.getvalue()
            self.assertIn("doctor_receipt: stale", rendered)
            self.assertNotIn("receipt_fingerprint", rendered)
            self.assertNotIn(str(ready_path), rendered)

    def test_parse_args_accepts_receipt(self):
        original_argv = sys.argv[:]
        try:
            sys.argv = [
                "cursor_handoff.py",
                "--receipt",
                "data/andrea-doctor-receipt.json",
                "--diagnose",
                "--json",
            ]
            parsed = MODULE.parse_args()
            self.assertEqual(parsed.receipt, "data/andrea-doctor-receipt.json")
            self.assertTrue(parsed.diagnose)
        finally:
            sys.argv = original_argv


if __name__ == "__main__":
    unittest.main()
