import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout


SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_openclaw.py"
RECEIPT_SCRIPT = SCRIPT_PATH.parent / "andrea_doctor_receipt.py"
SPEC = importlib.util.spec_from_file_location("cursor_openclaw", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["cursor_openclaw"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]
RECEIPT_SPEC = importlib.util.spec_from_file_location("andrea_doctor_receipt", RECEIPT_SCRIPT)
RECEIPT = importlib.util.module_from_spec(RECEIPT_SPEC)
assert RECEIPT_SPEC and RECEIPT_SPEC.loader
sys.modules.setdefault("andrea_doctor_receipt", RECEIPT)
RECEIPT_SPEC.loader.exec_module(RECEIPT)  # type: ignore[attr-defined]


def _cfg() -> MODULE.Config:
    return MODULE.Config(
        base_url="https://api.cursor.com",
        api_key="k",
        auth_mode="auto",
        timeout_seconds=30,
        retries=0,
        retry_backoff_seconds=0.0,
        output_json=True,
    )


def _build_receipt(*, safe: bool, security: str = "passed") -> dict:
    return RECEIPT.build_receipt(
        {
            "grade": "A",
            "readiness_plan": {
                "safe_for_autonomous_ops": safe,
                "blocker_count": 0 if safe else 1,
                "who_acts_first": "coding_agent" if safe else "owner",
                "next_action": "Continue the assigned offline test." if safe else "Stop.",
                "andrea_next_action": "Keep the draft pending.",
                "coding_agent_next_action": "Run offline verification." if safe else "Wait.",
                "owner_next_action": "No owner setup is required." if safe else "Restore security.",
                "holds": ["Do not send any live message."],
                "routing": {
                    "andrea": "offline only",
                    "coding_agent": "offline code and tests only",
                    "owner": "owner-gated actions only",
                },
                "actions": [],
            },
        },
        security_status=security,
        reliability_status="passed",
        openclaw_status="skipped_offline",
        exit_code=0 if safe and security == "passed" else 1,
    )


class CursorOpenClawTests(unittest.TestCase):
    def test_coerce_artifact_paths(self):
        self.assertEqual(MODULE._coerce_artifact_paths(["a.txt", " b.txt "]), ["a.txt", "b.txt"])
        payload = {
            "artifacts": [
                {"path": "reports/out.md"},
                {"artifactPath": "build/log.txt"},
                {"name": "screenshot.png"},
                {"id": "artifact-1"},
            ]
        }
        self.assertEqual(
            MODULE._coerce_artifact_paths(payload),
            ["reports/out.md", "build/log.txt", "screenshot.png", "artifact-1"],
        )
        self.assertEqual(MODULE._coerce_artifact_paths({"unexpected": 1}), [])

    def test_safe_index_name(self):
        self.assertEqual(MODULE._safe_index_name("bc-123"), "bc-123")
        self.assertEqual(MODULE._safe_index_name("bc 123/abc"), "bc-123-abc")
        self.assertEqual(MODULE._safe_index_name("///"), "agent")

    def test_build_artifact_index_markdown(self):
        md = MODULE._build_artifact_index_markdown(
            "bc-1",
            ["a.txt", "b.txt"],
            {"a.txt": "https://example.com/a"},
        )
        self.assertIn("Cursor Artifact Index", md)
        self.assertIn("<code>a.txt</code>", md)
        self.assertIn("[link](https://example.com/a)", md)
        self.assertIn("<code>b.txt</code>", md)
        self.assertIn("_(not requested or unavailable)_", md)

    def test_build_artifact_index_markdown_escapes_api_text(self):
        md = MODULE._build_artifact_index_markdown(
            "bc-1",
            ["report|name<script>\nnext.md"],
            {"report|name<script>\nnext.md": "https://example.com/a)b|c"},
        )
        self.assertIn("report&#124;name&lt;script&gt; next.md", md)
        self.assertIn("https://example.com/a%29b%7Cc", md)
        self.assertNotIn("<script>", md)

    def test_artifact_index_download_urls_are_opt_in(self):
        original_argv = sys.argv[:]
        try:
            sys.argv = ["cursor_openclaw.py", "artifact-index", "--id", "bc-1"]
            parsed = MODULE.parse_args()
            self.assertFalse(parsed.include_download_urls)
        finally:
            sys.argv = original_argv

    def test_parse_bool(self):
        self.assertTrue(MODULE.parse_bool("true"))
        self.assertTrue(MODULE.parse_bool("YES"))
        self.assertFalse(MODULE.parse_bool("false"))
        with self.assertRaises(ValueError):
            MODULE.parse_bool("maybe")

    def test_normalize_base_url(self):
        self.assertEqual(MODULE.normalize_base_url("https://api.cursor.com/"), "https://api.cursor.com")
        self.assertEqual(MODULE.normalize_base_url(""), "https://api.cursor.com")
        self.assertEqual(MODULE.normalize_base_url("http://localhost:8080/"), "http://localhost:8080")
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("ftp://example.com")
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("not-a-url")

    def test_validate_command_args_create_poll(self):
        bad = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=-1,
            poll_interval_seconds=1.0,
            prompt="x",
            intent=None,
            triage_repo="",
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad)
        bad2 = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=-0.5,
            prompt="x",
            intent=None,
            triage_repo="",
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad2)
        ok = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=0.0,
            prompt="x",
            intent=None,
            triage_repo="",
        )
        MODULE.validate_command_args(ok)
        ok_intent = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=0.0,
            prompt="",
            intent="brief",
            triage_repo="",
        )
        MODULE.validate_command_args(ok_intent)
        skip = types.SimpleNamespace(command="whoami")
        MODULE.validate_command_args(skip)

    def test_validate_command_args_branch_newline(self):
        bad = types.SimpleNamespace(
            command="create-agent",
            branch_name="evil\ninj",
            poll_attempts=0,
            poll_interval_seconds=0.0,
            prompt="x",
            intent=None,
            triage_repo="",
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad)

    def test_validate_command_args_create_needs_body(self):
        bad = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=0.0,
            prompt="",
            intent=None,
            triage_repo="",
        )
        with self.assertRaises(ValueError) as ctx:
            MODULE.validate_command_args(bad)
        self.assertIn("prompt", str(ctx.exception).lower())

    def test_validate_common_args(self):
        ok = types.SimpleNamespace(timeout_seconds=30, retries=2, retry_backoff_seconds=0.5)
        MODULE.validate_common_args(ok)

        bad_timeout = types.SimpleNamespace(timeout_seconds=0, retries=2, retry_backoff_seconds=0.5)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_timeout)

        bad_retries = types.SimpleNamespace(timeout_seconds=30, retries=-1, retry_backoff_seconds=0.5)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_retries)

        bad_backoff = types.SimpleNamespace(timeout_seconds=30, retries=2, retry_backoff_seconds=-0.1)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_backoff)

    def test_build_create_payload_from_repository(self):
        class Args:
            prompt = "hello"
            repository = "https://github.com/foo/bar"
            ref = "main"
            pr_url = ""
            model = "default"
            branch_name = "cursor/test"
            auto_create_pr = False
            open_as_cursor_github_app = False
            skip_reviewer_request = False

        payload = MODULE.build_create_payload(Args())
        self.assertEqual(payload["source"]["repository"], "https://github.com/foo/bar")
        self.assertEqual(payload["source"]["ref"], "main")
        self.assertEqual(payload["target"]["branchName"], "cursor/test")
        self.assertFalse(payload["target"]["autoCreatePr"])

    def test_require_one_of_exclusive(self):
        with self.assertRaises(ValueError) as ctx:
            MODULE.require_one_of("https://github.com/a/b", "https://github.com/a/b/pull/1")
        self.assertIn("only one", str(ctx.exception).lower())
        with self.assertRaises(ValueError):
            MODULE.require_one_of("", "")

    def test_build_create_payload_from_pr(self):
        class Args:
            prompt = "hello"
            repository = ""
            ref = ""
            pr_url = "https://github.com/foo/bar/pull/1"
            model = "default"
            branch_name = "cursor/test"
            auto_create_pr = True
            open_as_cursor_github_app = True
            skip_reviewer_request = True

        payload = MODULE.build_create_payload(Args())
        self.assertEqual(payload["source"]["prUrl"], "https://github.com/foo/bar/pull/1")
        self.assertTrue(payload["target"]["autoCreatePr"])
        self.assertTrue(payload["target"]["openAsCursorGithubApp"])
        self.assertTrue(payload["target"]["skipReviewerRequest"])

    def test_normalize_github_remote(self):
        self.assertEqual(
            MODULE._normalize_github_remote("git@github.com:foo/bar.git"),
            "https://github.com/foo/bar",
        )
        self.assertEqual(
            MODULE._normalize_github_remote("https://github.com/foo/bar.git"),
            "https://github.com/foo/bar",
        )
        self.assertEqual(
            MODULE._normalize_github_remote("https://github.com/foo/bar/"),
            "https://github.com/foo/bar",
        )

    def test_stop_all_jobs_validation(self):
        cfg = MODULE.Config(
            base_url="https://api.cursor.com",
            api_key="k",
            auth_mode="auto",
            timeout_seconds=30,
            retries=0,
            retry_backoff_seconds=0.0,
            output_json=True,
        )
        args = types.SimpleNamespace(
            command="stop-all-jobs",
            limit="0",
            max_pages=10,
            repo=".",
            include_terminal=False,
            dry_run=False,
            yes=False,
        )
        with self.assertRaises(ValueError):
            MODULE.handle(cfg, args)

    def test_stop_all_jobs_dry_run_without_yes(self):
        cfg = MODULE.Config(
            base_url="https://api.cursor.com",
            api_key="k",
            auth_mode="auto",
            timeout_seconds=30,
            retries=0,
            retry_backoff_seconds=0.0,
            output_json=True,
        )
        args = types.SimpleNamespace(
            command="stop-all-jobs",
            limit="100",
            max_pages=2,
            repo=".",
            include_terminal=False,
            dry_run=False,
            yes=False,
        )

        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, query, body))
                if method == "GET" and path == "/v0/agents":
                    return (
                        200,
                        {
                            "agents": [
                                {
                                    "id": "ag_running",
                                    "status": "RUNNING",
                                    "source": {"repository": "https://github.com/foo/bar"},
                                    "target": {"url": "https://cursor.com/agents/ag_running"},
                                },
                                {
                                    "id": "ag_done",
                                    "status": "FINISHED",
                                    "source": {"repository": "https://github.com/foo/bar"},
                                    "target": {"url": "https://cursor.com/agents/ag_done"},
                                },
                                {
                                    "id": "ag_other",
                                    "status": "RUNNING",
                                    "source": {"repository": "https://github.com/foo/other"},
                                    "target": {"url": "https://cursor.com/agents/ag_other"},
                                },
                            ],
                            "cursor": "",
                        },
                        "{}",
                        "bearer",
                    )
                raise AssertionError(f"Unexpected request: {method} {path}")

        old_client = MODULE.CursorApiClient
        old_detect = MODULE._detect_repo_origin_url
        old_validate = MODULE.cursor_api_common.validate_agent_id
        MODULE.CursorApiClient = FakeClient
        MODULE._detect_repo_origin_url = lambda _p: "https://github.com/foo/bar"
        MODULE.cursor_api_common.validate_agent_id = lambda _aid, flag_name="--id": None
        try:
            status, payload = MODULE.handle(cfg, args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE._detect_repo_origin_url = old_detect
            MODULE.cursor_api_common.validate_agent_id = old_validate

        self.assertEqual(status, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["scanned"], 3)
        self.assertEqual(payload["matched"], 2)
        self.assertEqual(payload["eligible_to_stop"], 1)
        self.assertEqual(payload["skipped_terminal"], 1)
        self.assertEqual(len(payload["results"]), 0)
        self.assertEqual(len(payload["agents"]), 1)
        self.assertEqual(payload["agents"][0]["id"], "ag_running")
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "/v0/agents")

    def test_stop_all_jobs_execution_fails_closed_on_partial_error(self):
        cfg = MODULE.Config(
            base_url="https://api.cursor.com",
            api_key="k",
            auth_mode="auto",
            timeout_seconds=30,
            retries=0,
            retry_backoff_seconds=0.0,
            output_json=True,
        )
        args = types.SimpleNamespace(
            command="stop-all-jobs",
            limit="100",
            max_pages=1,
            repo=".",
            include_terminal=False,
            dry_run=False,
            yes=True,
        )
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path))
                if method == "GET" and path == "/v0/agents":
                    return (
                        200,
                        {
                            "agents": [
                                {
                                    "id": "ag_ok",
                                    "status": "RUNNING",
                                    "source": {"repository": "https://github.com/foo/bar"},
                                },
                                {
                                    "id": "ag_fail",
                                    "status": "RUNNING",
                                    "source": {"repository": "https://github.com/foo/bar"},
                                },
                            ],
                            "cursor": "",
                        },
                        "{}",
                        "bearer",
                    )
                if path.endswith("/ag_ok/stop"):
                    return 200, {"status": "STOPPED"}, "", "bearer"
                if path.endswith("/ag_fail/stop"):
                    return 503, {"error": "unavailable"}, "", "bearer"
                raise AssertionError(f"Unexpected request: {method} {path}")

        old_client = MODULE.CursorApiClient
        old_detect = MODULE._detect_repo_origin_url
        MODULE.CursorApiClient = FakeClient
        MODULE._detect_repo_origin_url = lambda _p: "https://github.com/foo/bar"
        try:
            status, payload = MODULE.handle(cfg, args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE._detect_repo_origin_url = old_detect

        self.assertEqual(status, 502)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["attempted"], 2)
        self.assertEqual(payload["stopped"], 1)
        self.assertEqual(payload["failed_to_stop"], 1)
        self.assertEqual(
            calls,
            [
                ("GET", "/v0/agents"),
                ("POST", "/v0/agents/ag_ok/stop"),
                ("POST", "/v0/agents/ag_fail/stop"),
            ],
        )

    def test_iter_agents_follows_official_next_cursor_for_two_pages(self):
        calls = []

        class FakeClient:
            def request(self, method, path, query=None, body=None):
                calls.append((method, path, query, body))
                if len(calls) == 1:
                    return (
                        200,
                        {"agents": [{"id": "ag_first"}], "nextCursor": "page-2"},
                        "{}",
                        "bearer",
                    )
                if len(calls) == 2:
                    return (
                        200,
                        {"agents": [{"id": "ag_second"}]},
                        "{}",
                        "bearer",
                    )
                raise AssertionError("Unexpected extra request")

        agents, complete = MODULE._iter_agents(FakeClient(), limit=1, max_pages=2)

        self.assertTrue(complete)
        self.assertEqual([agent["id"] for agent in agents], ["ag_first", "ag_second"])
        self.assertEqual(
            calls,
            [
                ("GET", "/v0/agents", {"limit": "1"}, None),
                ("GET", "/v0/agents", {"limit": "1", "cursor": "page-2"}, None),
            ],
        )

    def test_iter_agents_rejects_conflicting_cursor_aliases(self):
        class FakeClient:
            def request(self, method, path, query=None, body=None):
                return (
                    200,
                    {
                        "agents": [{"id": "ag_first"}],
                        "nextCursor": "official-page-2",
                        "cursor": "different-page-2",
                    },
                    "{}",
                    "bearer",
                )

        with self.assertRaisesRegex(RuntimeError, "conflicting pagination cursors"):
            MODULE._iter_agents(FakeClient(), limit=1, max_pages=2)

    def test_stop_all_jobs_refuses_truncated_scan_before_any_stop(self):
        cfg = MODULE.Config(
            base_url="https://api.cursor.com",
            api_key="k",
            auth_mode="auto",
            timeout_seconds=30,
            retries=0,
            retry_backoff_seconds=0.0,
            output_json=True,
        )
        args = types.SimpleNamespace(
            command="stop-all-jobs",
            limit="1",
            max_pages=1,
            repo=".",
            include_terminal=False,
            dry_run=False,
            yes=True,
        )
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path))
                if method == "GET" and path == "/v0/agents":
                    return (
                        200,
                        {
                            "agents": [
                                {
                                    "id": "ag_first",
                                    "status": "RUNNING",
                                    "source": {"repository": "https://github.com/foo/bar"},
                                }
                            ],
                            "nextCursor": "another-page",
                        },
                        "{}",
                        "bearer",
                    )
                raise AssertionError(f"Unexpected request: {method} {path}")

        old_client = MODULE.CursorApiClient
        old_detect = MODULE._detect_repo_origin_url
        MODULE.CursorApiClient = FakeClient
        MODULE._detect_repo_origin_url = lambda _p: "https://github.com/foo/bar"
        try:
            status, payload = MODULE.handle(cfg, args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE._detect_repo_origin_url = old_detect

        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["scan_complete"])
        self.assertEqual(payload["attempted"], 0)
        self.assertEqual(payload["stopped"], 0)
        self.assertIn("no stops were attempted", payload["note"])
        self.assertEqual(calls, [("GET", "/v0/agents")])

    def test_parse_args_accepts_receipt_on_diagnose_and_create_agent(self):
        original_argv = sys.argv[:]
        try:
            sys.argv = [
                "cursor_openclaw.py",
                "--json",
                "diagnose",
                "--receipt",
                "data/andrea-doctor-receipt.json",
            ]
            parsed = MODULE.parse_args()
            self.assertEqual(parsed.command, "diagnose")
            self.assertEqual(parsed.receipt, "data/andrea-doctor-receipt.json")
            sys.argv = [
                "cursor_openclaw.py",
                "create-agent",
                "--prompt",
                "p",
                "--repository",
                "https://github.com/foo/bar",
                "--branch-name",
                "cursor/test",
                "--receipt",
                "data/andrea-doctor-receipt.json",
                "--dry-run",
            ]
            parsed = MODULE.parse_args()
            self.assertEqual(parsed.command, "create-agent")
            self.assertEqual(parsed.receipt, "data/andrea-doctor-receipt.json")
            self.assertTrue(parsed.dry_run)
        finally:
            sys.argv = original_argv

    def test_consult_absent_is_not_a_create_agent_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            consult = MODULE.consult_doctor_receipt(
                explicit="",
                local_repo=root,
                cwd=root,
                environ={},
            )
            self.assertFalse(consult["consulted"])
            self.assertEqual(consult["receipt_state"], "absent")
            self.assertIsNone(MODULE.live_create_agent_block_reason(consult))

    def test_consult_does_not_autodiscover_tmp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            tmp_receipt = pathlib.Path("/tmp") / f"andrea-doctor-receipt-{root.name}.json"
            try:
                RECEIPT.write_receipt(tmp_receipt, _build_receipt(safe=False, security="failed"))
                consult = MODULE.consult_doctor_receipt(
                    explicit="",
                    local_repo=root,
                    cwd=root,
                    environ={},
                )
                self.assertFalse(consult["consulted"])
                self.assertEqual(consult["receipt_source"], "absent")
            finally:
                if tmp_receipt.exists():
                    tmp_receipt.unlink()

    def test_consult_missing_explicit_blocks_live_create_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            missing = root / "missing.json"
            consult = MODULE.consult_doctor_receipt(
                explicit=str(missing),
                local_repo=root,
                cwd=root,
                environ={},
            )
            self.assertTrue(consult["consulted"])
            self.assertEqual(consult["receipt_state"], "missing")
            self.assertEqual(consult["who_acts_first"], "coding_agent")
            self.assertTrue(consult["may_continue_offline_code"])
            self.assertIsNotNone(MODULE.live_create_agent_block_reason(consult))

    def test_consult_stale_and_owner_hold_block_live_api(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            ready_path = root / "ready.json"
            failed_path = root / "failed.json"
            RECEIPT.write_receipt(ready_path, _build_receipt(safe=True))
            RECEIPT.write_receipt(failed_path, _build_receipt(safe=False, security="failed"))
            current = MODULE.consult_doctor_receipt(
                explicit=str(ready_path),
                local_repo=root,
                cwd=root,
                now=ready_path.stat().st_mtime + 4,
                environ={},
            )
            self.assertTrue(current["safe_for_autonomous_ops"])
            self.assertIsNone(MODULE.live_create_agent_block_reason(current))
            stale = MODULE.consult_doctor_receipt(
                explicit=str(ready_path),
                local_repo=root,
                cwd=root,
                now=ready_path.stat().st_mtime + RECEIPT.RECEIPT_MAX_AGE_SECONDS + 1,
                environ={},
            )
            self.assertEqual(stale["receipt_state"], "stale")
            self.assertIn("current authority", MODULE.live_create_agent_block_reason(stale) or "")
            owner_hold = MODULE.consult_doctor_receipt(
                explicit=str(failed_path),
                local_repo=root,
                cwd=root,
                now=failed_path.stat().st_mtime + 4,
                environ={},
            )
            self.assertIsNotNone(MODULE.live_create_agent_block_reason(owner_hold))

    def test_diagnose_includes_doctor_receipt_packet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "receipt.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=True))
            args = types.SimpleNamespace(
                command="diagnose",
                show_key=False,
                receipt=str(receipt_path),
            )
            status, payload = MODULE.handle(_cfg(), args)
            self.assertEqual(status, 0)
            consult = payload["doctor_receipt"]
            self.assertTrue(consult["consulted"])
            self.assertEqual(consult["receipt_state"], "current")
            self.assertNotIn("receipt_fingerprint", consult)
            self.assertNotIn(str(receipt_path), json.dumps(consult))

    def test_create_agent_dry_run_reports_receipt_without_posting(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError("dry-run must not call the API")

        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            args = types.SimpleNamespace(
                command="create-agent",
                prompt="hello",
                intent=None,
                triage_repo="",
                repository="https://github.com/foo/bar",
                ref="main",
                pr_url="",
                model="default",
                branch_name="cursor/test",
                auto_create_pr=False,
                open_as_cursor_github_app=False,
                skip_reviewer_request=False,
                poll_attempts=0,
                poll_interval_seconds=0.0,
                dry_run=True,
                receipt=str(receipt_path),
            )
            old_client = MODULE.CursorApiClient
            MODULE.CursorApiClient = FakeClient
            try:
                status, payload = MODULE.handle(_cfg(), args)
            finally:
                MODULE.CursorApiClient = old_client
        self.assertEqual(status, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["payload"]["source"]["repository"], "https://github.com/foo/bar")
        self.assertTrue(payload["doctor_receipt"]["consulted"])
        self.assertIsNotNone(payload["receipt_would_block"])
        self.assertEqual(calls, [])

    def test_create_agent_live_blocks_consulted_receipt_without_http(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError("blocked create-agent must not POST")

        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            args = types.SimpleNamespace(
                command="create-agent",
                prompt="hello",
                intent=None,
                triage_repo="",
                repository="https://github.com/foo/bar",
                ref="main",
                pr_url="",
                model="default",
                branch_name="cursor/test",
                auto_create_pr=False,
                open_as_cursor_github_app=False,
                skip_reviewer_request=False,
                poll_attempts=0,
                poll_interval_seconds=0.0,
                dry_run=False,
                receipt=str(receipt_path),
            )
            old_client = MODULE.CursorApiClient
            MODULE.CursorApiClient = FakeClient
            try:
                with self.assertRaises(MODULE.DoctorReceiptBlocked) as ctx:
                    MODULE.handle(_cfg(), args)
            finally:
                MODULE.CursorApiClient = old_client
        self.assertIn("current authority", str(ctx.exception))
        self.assertEqual(ctx.exception.payload["doctor_receipt"]["who_acts_first"], "owner")
        self.assertNotIn(str(receipt_path), json.dumps(ctx.exception.payload))
        self.assertEqual(calls, [])

    def test_create_agent_live_absent_receipt_still_posts(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                return 200, {"id": "bc-1", "status": "CREATING"}, "{}", "bearer"

        args = types.SimpleNamespace(
            command="create-agent",
            prompt="hello",
            intent=None,
            triage_repo="",
            repository="https://github.com/foo/bar",
            ref="main",
            pr_url="",
            model="default",
            branch_name="cursor/test",
            auto_create_pr=False,
            open_as_cursor_github_app=False,
            skip_reviewer_request=False,
            poll_attempts=0,
            poll_interval_seconds=0.0,
            dry_run=False,
            receipt="",
        )
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: RECEIPT.absent_handoff_consult()
        try:
            status, payload = MODULE.handle(_cfg(), args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
        self.assertEqual(status, 200)
        self.assertEqual(payload["response"]["id"], "bc-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/v0/agents")

    def test_main_create_agent_receipt_block_exits_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            original_argv = sys.argv[:]
            env_key = os.environ.get("CURSOR_API_KEY")
            os.environ["CURSOR_API_KEY"] = "dummy_test_key"
            sys.argv = [
                "cursor_openclaw.py",
                "--json",
                "create-agent",
                "--prompt",
                "hello",
                "--repository",
                "https://github.com/foo/bar",
                "--ref",
                "main",
                "--branch-name",
                "cursor/test",
                "--receipt",
                str(receipt_path),
            ]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = MODULE.main()
            finally:
                sys.argv = original_argv
                if env_key is None:
                    os.environ.pop("CURSOR_API_KEY", None)
                else:
                    os.environ["CURSOR_API_KEY"] = env_key
            rendered = buf.getvalue()
            payload = json.loads(rendered)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertIn("current authority", payload["error"])
            self.assertEqual(payload["doctor_receipt"]["receipt_state"], "current")
            self.assertNotIn("receipt_fingerprint", rendered)
            self.assertNotIn(str(receipt_path), rendered)

    def test_parse_args_accepts_receipt_and_dry_run_on_followup(self):
        original_argv = sys.argv[:]
        try:
            sys.argv = [
                "cursor_openclaw.py",
                "--json",
                "followup",
                "--id",
                "bc-abc123",
                "--prompt",
                "continue",
                "--receipt",
                "data/andrea-doctor-receipt.json",
                "--dry-run",
            ]
            parsed = MODULE.parse_args()
            self.assertEqual(parsed.command, "followup")
            self.assertEqual(parsed.receipt, "data/andrea-doctor-receipt.json")
            self.assertTrue(parsed.dry_run)
        finally:
            sys.argv = original_argv

    def test_followup_dry_run_reports_receipt_without_http(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError("followup dry-run must not call the API")

        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            args = types.SimpleNamespace(
                command="followup",
                id="bc-abc123",
                prompt="please continue",
                dry_run=True,
                receipt=str(receipt_path),
            )
            old_client = MODULE.CursorApiClient
            MODULE.CursorApiClient = FakeClient
            try:
                status, payload = MODULE.handle(_cfg(), args)
            finally:
                MODULE.CursorApiClient = old_client
        self.assertEqual(status, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["agent_state"], "not_checked")
        self.assertTrue(payload["doctor_receipt"]["consulted"])
        self.assertIsNotNone(payload["receipt_would_block"])
        self.assertEqual(payload["followup_would_block"], payload["receipt_would_block"])
        self.assertFalse(payload["followup_ready"])
        self.assertNotIn(str(receipt_path), json.dumps(payload))
        self.assertEqual(calls, [])

    def test_followup_dry_run_clear_receipt_still_not_ready(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError("followup dry-run must not call the API")

        args = types.SimpleNamespace(
            command="followup",
            id="bc-abc123",
            prompt="please continue",
            dry_run=True,
            receipt="",
        )
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: RECEIPT.absent_handoff_consult()
        try:
            status, payload = MODULE.handle(_cfg(), args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
        self.assertEqual(status, 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["agent_state"], "not_checked")
        self.assertIsNone(payload["receipt_would_block"])
        self.assertEqual(
            payload["followup_would_block"],
            MODULE.cursor_api_common.FOLLOWUP_AGENT_NOT_CHECKED,
        )
        self.assertFalse(payload["followup_ready"])
        self.assertNotEqual(payload["followup_would_block"], payload["receipt_would_block"])
        self.assertEqual(calls, [])

    def test_followup_live_blocks_consulted_receipt_without_http(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError("blocked followup must not call the API")

        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            args = types.SimpleNamespace(
                command="followup",
                id="bc-abc123",
                prompt="please continue",
                dry_run=False,
                receipt=str(receipt_path),
            )
            old_client = MODULE.CursorApiClient
            MODULE.CursorApiClient = FakeClient
            try:
                with self.assertRaises(MODULE.DoctorReceiptBlocked) as ctx:
                    MODULE.handle(_cfg(), args)
            finally:
                MODULE.CursorApiClient = old_client
        self.assertIn("current authority", str(ctx.exception))
        self.assertEqual(ctx.exception.payload["agent_state"], "not_checked")
        self.assertFalse(ctx.exception.payload["followup_ready"])
        self.assertEqual(
            ctx.exception.payload["followup_would_block"],
            ctx.exception.payload["error"],
        )
        self.assertNotIn(str(receipt_path), json.dumps(ctx.exception.payload))
        self.assertEqual(calls, [])

    def test_followup_live_blocks_missing_and_stale_agent_without_post(self):
        for status_code, agent_body, expected_state in (
            (404, {"error": "not found"}, "missing"),
            (200, {"id": "bc-abc123", "status": "FINISHED"}, "stale"),
        ):
            calls = []

            class FakeClient:
                def __init__(self, _cfg):
                    pass

                def request(self, method, path, query=None, body=None):
                    calls.append((method, path, body))
                    if method == "GET" and path == "/v0/agents/bc-abc123":
                        return status_code, agent_body, "{}", "bearer"
                    raise AssertionError("blocked followup must not POST")

            args = types.SimpleNamespace(
                command="followup",
                id="bc-abc123",
                prompt="please continue",
                dry_run=False,
                receipt="",
            )
            old_client = MODULE.CursorApiClient
            old_consult = MODULE.consult_doctor_receipt
            MODULE.CursorApiClient = FakeClient
            MODULE.consult_doctor_receipt = lambda **_kwargs: RECEIPT.absent_handoff_consult()
            try:
                with self.assertRaises(MODULE.AgentStateBlocked) as ctx:
                    MODULE.handle(_cfg(), args)
            finally:
                MODULE.CursorApiClient = old_client
                MODULE.consult_doctor_receipt = old_consult
            self.assertEqual(ctx.exception.payload["agent_state"], expected_state)
            self.assertEqual(ctx.exception.payload["agent"]["id"], "bc-abc123")
            self.assertFalse(ctx.exception.payload["followup_ready"])
            self.assertEqual(
                ctx.exception.payload["followup_would_block"],
                ctx.exception.payload["error"],
            )
            self.assertEqual(calls, [("GET", "/v0/agents/bc-abc123", None)])

    def test_followup_live_absent_receipt_posts_only_when_agent_is_running(self):
        calls = []

        class FakeClient:
            def __init__(self, _cfg):
                pass

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                if method == "GET":
                    return 200, {"id": "bc-abc123", "status": "RUNNING"}, "{}", "bearer"
                return 200, {"ok": True}, "{}", "bearer"

        args = types.SimpleNamespace(
            command="followup",
            id="bc-abc123",
            prompt="please continue",
            dry_run=False,
            receipt="",
        )
        old_client = MODULE.CursorApiClient
        old_consult = MODULE.consult_doctor_receipt
        MODULE.CursorApiClient = FakeClient
        MODULE.consult_doctor_receipt = lambda **_kwargs: RECEIPT.absent_handoff_consult()
        try:
            status, payload = MODULE.handle(_cfg(), args)
        finally:
            MODULE.CursorApiClient = old_client
            MODULE.consult_doctor_receipt = old_consult
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent_state"], "running")
        self.assertEqual(payload["agent"]["status"], "RUNNING")
        self.assertTrue(payload["followup_ready"])
        self.assertIsNone(payload["followup_would_block"])
        self.assertEqual(payload["doctor_receipt"]["receipt_source"], "absent")
        self.assertNotIn("receipt_fingerprint", json.dumps(payload))
        self.assertEqual(calls[0], ("GET", "/v0/agents/bc-abc123", None))
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][1], "/v0/agents/bc-abc123/followup")
        self.assertEqual(calls[1][2], {"prompt": {"text": "please continue"}})

    def test_main_followup_receipt_block_exits_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_path = pathlib.Path(temp_dir) / "failed.json"
            RECEIPT.write_receipt(receipt_path, _build_receipt(safe=False, security="failed"))
            original_argv = sys.argv[:]
            env_key = os.environ.get("CURSOR_API_KEY")
            os.environ["CURSOR_API_KEY"] = "dummy_test_key"
            sys.argv = [
                "cursor_openclaw.py",
                "--json",
                "followup",
                "--id",
                "bc-abc123",
                "--prompt",
                "please continue",
                "--receipt",
                str(receipt_path),
            ]
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = MODULE.main()
            finally:
                sys.argv = original_argv
                if env_key is None:
                    os.environ.pop("CURSOR_API_KEY", None)
                else:
                    os.environ["CURSOR_API_KEY"] = env_key
            rendered = buf.getvalue()
            payload = json.loads(rendered)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertIn("current authority", payload["error"])
            self.assertEqual(payload["agent_state"], "not_checked")
            self.assertNotIn("receipt_fingerprint", rendered)
            self.assertNotIn(str(receipt_path), rendered)


if __name__ == "__main__":
    unittest.main()
