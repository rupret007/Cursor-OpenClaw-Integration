import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_handoff.py"
)
SPEC = importlib.util.spec_from_file_location("cursor_handoff", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["cursor_handoff"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class CursorHandoffTests(unittest.TestCase):
    def _run_status(self, response, *, as_json=True, http_status=200, error=None):
        client = mock.Mock(spec=["request"])
        client.request.return_value = (http_status, response, "raw response", "bearer")
        client.request.side_effect = error
        argv = ["cursor_handoff.py", "--mode", "api", "--op", "status", "--agent-id", "bc-run1"]
        if as_json:
            argv.append("--json")
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {"CURSOR_API_KEY": "dummy_test_key"}, clear=True),
            mock.patch.object(MODULE.env_loader, "merge_dotenv_paths", return_value=[]),
            mock.patch.object(MODULE, "detect_cli_binary", return_value=None),
            mock.patch.object(MODULE, "consult_doctor_receipt", return_value={"consulted": False}),
            mock.patch.object(MODULE, "CursorApiClient", return_value=client),
            redirect_stdout(output),
        ):
            code = MODULE.main()
        client.request.assert_called_once_with("GET", "/v0/agents/bc-run1")
        text = output.getvalue()
        return code, json.loads(text) if as_json else text

    def test_status_json_separates_agent_state_from_http_success(self):
        for state in ["CREATING", "PENDING", "RUNNING", "FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"]:
            with self.subTest(state=state):
                response = {"id": "bc-run1", "status": state}
                code, payload = self._run_status(response)
                self.assertEqual(code, MODULE.EXIT_OK)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["status"], 200)
                self.assertEqual(payload["response"], response)
                self.assertEqual(payload["agent_status"], state)
                self.assertTrue(payload["status_verified"])
                self.assertTrue(payload["next_action"])
                self.assertNotIn("submitted", payload)

    def test_status_text_reports_failed_agent_and_read_only_next_step(self):
        code, text = self._run_status({"id": "bc-run1", "status": "FAILED"}, as_json=False)
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertIn("Agent status: FAILED", text)
        self.assertIn("HTTP status: 200", text)
        self.assertIn("Agent ID: bc-run1", text)
        self.assertIn("conversation and artifacts", text)
        self.assertIn("before deciding whether to retry", text)
        self.assertNotIn("submitted", text)
        self.assertNotIn("None", text)

    def test_status_does_not_trust_missing_mismatched_or_unrecognized_evidence(self):
        for response in [
            {}, {"status": "FINISHED"}, {"id": "bc-other", "status": "FINISHED"},
            {"id": "bc-run1"}, {"id": "bc-run1", "status": "NEW_PROVIDER_STATE"},
            {"id": "bc-run1", "status": ["FINISHED"]},
            {"id": "bc-run1", "status": "FINISHED", "_non_json_response": True},
        ]:
            with self.subTest(response=response):
                _, payload = self._run_status(response)
                self.assertEqual(payload["agent_status"], "UNKNOWN")
                self.assertFalse(payload["status_verified"])
                self.assertIn("unverified", payload["next_action"])

    def test_status_normalizes_known_state_and_does_not_claim_verified_work(self):
        _, payload = self._run_status({"id": "bc-run1", "status": " finished "})
        self.assertEqual(payload["agent_status"], "FINISHED")
        self.assertIn("Review", payload["next_action"])
        self.assertIn("not verified", payload["next_action"])

    def test_status_http_or_transport_failure_never_claims_a_handoff(self):
        for options in [{"http_status": 404}, {"http_status": 503}, {"error": OSError("offline")}]:
            with self.subTest(options=options):
                code, text = self._run_status({}, as_json=False, **options)
                self.assertEqual(code, MODULE.EXIT_API)
                self.assertIn("Agent status check failed", text)
                self.assertIn("bc-run1", text)
                self.assertNotIn("Handoff", text)

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
                cwd=root,
                environ={},
            )
            self.assertFalse(absent["consulted"])
            self.assertIsNone(MODULE.live_handoff_block_reason(absent, "api"))
            missing = MODULE.consult_doctor_receipt(
                explicit=str(root / "missing.json"),
                local_repo=root,
                search_roots=[repo_root],
                cwd=root,
                environ={},
            )
            self.assertTrue(missing["consulted"])
            self.assertEqual(missing["receipt_state"], "missing")
            self.assertEqual(missing["who_acts_first"], "coding_agent")
            self.assertTrue(missing["may_continue_offline_code"])
            self.assertIsNotNone(MODULE.live_handoff_block_reason(missing, "api"))
            self.assertIsNone(MODULE.live_handoff_block_reason(missing, "cli"))
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
            dry_text = io.StringIO()
            with redirect_stdout(dry_text):
                MODULE.emit_text(
                    {
                        "dry_run": True,
                        "backend": "api",
                        "mode_requested": "api",
                        "read_only": True,
                        "branch": "cursor/test",
                        "repo_input": str(root),
                        "doctor_receipt": current,
                        "receipt_would_block": None,
                        **MODULE.cursor_api_common.followup_dry_run_fields(None),
                    }
                )
            dry_rendered = dry_text.getvalue()
            self.assertIn("followup_ready: False", dry_rendered)
            self.assertIn("followup_would_block:", dry_rendered)
            self.assertIn("did not check", dry_rendered)
            self.assertIn("agent_state: not_checked", dry_rendered)
            self.assertNotIn("receipt_fingerprint", dry_rendered)

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

    def test_followup_dry_run_marks_agent_not_checked(self):
        original_argv = sys.argv[:]
        sys.argv = [
            "cursor_handoff.py",
            "--mode",
            "api",
            "--op",
            "followup",
            "--agent-id",
            "bc-abc123",
            "--prompt",
            "continue",
            "--dry-run",
            "--json",
        ]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = MODULE.main()
        finally:
            sys.argv = original_argv
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["agent_state"], "not_checked")
        self.assertFalse(payload["followup_ready"])
        self.assertEqual(
            payload["followup_would_block"],
            MODULE.cursor_api_common.FOLLOWUP_AGENT_NOT_CHECKED,
        )
        self.assertNotEqual(payload["followup_would_block"], payload["receipt_would_block"])

    def test_followup_live_blocks_stale_agent_without_post(self):
        calls = []

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, path, body=None, query=None):
                calls.append((method, path, body))
                if method == "GET" and path == "/v0/agents/bc-abc123":
                    return 200, {"id": "bc-abc123", "status": "FINISHED"}, "{}", "bearer"
                raise AssertionError("blocked followup must not POST")

        original_argv = sys.argv[:]
        env_key = os.environ.get("CURSOR_API_KEY")
        os.environ["CURSOR_API_KEY"] = "dummy_test_key"
        sys.argv = [
            "cursor_handoff.py",
            "--mode",
            "api",
            "--op",
            "followup",
            "--agent-id",
            "bc-abc123",
            "--prompt",
            "continue",
            "--json",
        ]
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: {
            "consulted": False,
            "receipt_source": "absent",
            "receipt_state": "absent",
            "receipt_verified": False,
            "fresh": False,
            "overall_status": "",
            "blocked_reason": "",
            "failed_stages": [],
            "grade": "",
            "who_acts_first": "",
            "safe_for_autonomous_ops": False,
            "may_continue_offline_code": True,
            "must_wait_for_owner": False,
            "next_action": "",
            "reason": "receipt_not_consulted",
        }
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = MODULE.main()
        finally:
            sys.argv = original_argv
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
            if env_key is None:
                os.environ.pop("CURSOR_API_KEY", None)
            else:
                os.environ["CURSOR_API_KEY"] = env_key
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, MODULE.EXIT_PREREQ)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["agent_state"], "stale")
        self.assertFalse(payload["followup_ready"])
        self.assertEqual(payload["followup_would_block"], payload["error"])
        self.assertIn("FINISHED", payload["error"])
        self.assertEqual(calls, [("GET", "/v0/agents/bc-abc123", None)])

    def test_followup_live_posts_when_agent_is_running(self):
        calls = []

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, path, body=None, query=None):
                calls.append((method, path, body))
                if method == "GET":
                    return 200, {"id": "bc-abc123", "status": "RUNNING"}, "{}", "bearer"
                return 200, {"ok": True}, "{}", "bearer"

        original_argv = sys.argv[:]
        env_key = os.environ.get("CURSOR_API_KEY")
        os.environ["CURSOR_API_KEY"] = "dummy_test_key"
        sys.argv = [
            "cursor_handoff.py",
            "--mode",
            "api",
            "--op",
            "followup",
            "--agent-id",
            "bc-abc123",
            "--prompt",
            "continue",
            "--json",
        ]
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: {
            "consulted": False,
            "receipt_source": "absent",
            "receipt_state": "absent",
            "receipt_verified": False,
            "fresh": False,
            "overall_status": "",
            "blocked_reason": "",
            "failed_stages": [],
            "grade": "",
            "who_acts_first": "",
            "safe_for_autonomous_ops": False,
            "may_continue_offline_code": True,
            "must_wait_for_owner": False,
            "next_action": "",
            "reason": "receipt_not_consulted",
        }
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = MODULE.main()
        finally:
            sys.argv = original_argv
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
            if env_key is None:
                os.environ.pop("CURSOR_API_KEY", None)
            else:
                os.environ["CURSOR_API_KEY"] = env_key
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agent_state"], "running")
        self.assertTrue(payload["followup_ready"])
        self.assertIsNone(payload["followup_would_block"])
        self.assertEqual(payload["doctor_receipt"]["receipt_source"], "absent")
        self.assertEqual(calls[0], ("GET", "/v0/agents/bc-abc123", None))
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][1], "/v0/agents/bc-abc123/followup")
    def _run_submit_api(self, poll_status):
        calls = []

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def create_agent(self, payload):
                calls.append(("create", payload))
                return (
                    200,
                    {"id": "bc-run1", "status": "RUNNING", "target": {"url": "https://cursor.com/agents/bc-run1"}},
                    "{}",
                    "bearer",
                )

            def get_agent(self, aid):
                calls.append(("poll", aid))
                return 200, {"id": aid, "status": poll_status, "target": {"url": "https://cursor.com/agents/bc-run1"}}, "{}", "bearer"

        original_argv = sys.argv[:]
        env_key = os.environ.get("CURSOR_API_KEY")
        os.environ["CURSOR_API_KEY"] = "dummy_test_key"
        sys.argv = [
            "cursor_handoff.py",
            "--repo",
            "owner/repo",
            "--prompt",
            "Review the module",
            "--mode",
            "api",
            "--read-only",
            "true",
            "--poll-max-attempts",
            "1",
            "--poll-interval-seconds",
            "0",
            "--json",
        ]
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: {
            "consulted": False,
            "may_continue_offline_code": True,
            "safe_for_autonomous_ops": False,
            "receipt_source": "absent",
            "receipt_state": "absent",
        }
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = MODULE.main()
        finally:
            sys.argv = original_argv
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
            if env_key is None:
                os.environ.pop("CURSOR_API_KEY", None)
            else:
                os.environ["CURSOR_API_KEY"] = env_key
        return code, json.loads(buf.getvalue()), calls

    def test_submit_api_reports_terminal_failure(self):
        code, payload, calls = self._run_submit_api("FAILED")
        self.assertEqual(code, MODULE.EXIT_API)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["submitted"])
        self.assertEqual(payload["agent_id"], "bc-run1")
        self.assertEqual(payload["status"], "FAILED")
        self.assertIn("FAILED", payload["error"])
        self.assertEqual(calls[0][0], "create")
        self.assertEqual(calls[1][0], "poll")

    def test_submit_api_ok_when_finished(self):
        code, payload, _calls = self._run_submit_api("FINISHED")
        self.assertEqual(code, MODULE.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "FINISHED")
        self.assertNotIn("error", payload)

    def test_emit_text_failure_shows_agent_url(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            MODULE.emit_text(
                {
                    "ok": False,
                    "submitted": True,
                    "error": "Cursor Cloud agent bc-x ended in FAILED without completing the handoff.",
                    "agent_id": "bc-x",
                    "status": "FAILED",
                    "agent_url": "https://cursor.com/agents/bc-x",
                }
            )
        out = buf.getvalue()
        self.assertIn("Handoff failed.", out)
        self.assertIn("agent_url: https://cursor.com/agents/bc-x", out)
        self.assertIn("status: FAILED", out)
        self.assertNotIn("Handoff submitted successfully", out)


if __name__ == "__main__":
    unittest.main()
