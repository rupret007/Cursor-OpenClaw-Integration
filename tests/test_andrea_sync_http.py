"""HTTP smoke tests for Andrea lockstep server (localhost, ephemeral DB)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.telegram_format import format_final_message  # noqa: E402
from services.andrea_sync.adapters import telegram as tg_adapt  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.experience_assurance import run_experience_assurance  # noqa: E402
from services.andrea_sync.schema import EventType  # noqa: E402
from services.andrea_sync.store import append_event, ensure_system_task, save_incident  # noqa: E402


class TestAndreaSyncHTTP(unittest.TestCase):
    _httpd: ThreadingHTTPServer
    _srv: object
    _port: int
    _thread: threading.Thread
    _dbpath: str

    @classmethod
    def setUpClass(cls) -> None:
        fd, cls._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._env_backup = {}
        for key in (
            "ANDREA_SYNC_DB",
            "ANDREA_SYNC_TELEGRAM_SECRET",
            "ANDREA_SYNC_INTERNAL_TOKEN",
            "ANDREA_SYNC_BACKGROUND_ENABLED",
            "ANDREA_SYNC_TELEGRAM_NOTIFIER",
            "TELEGRAM_BOT_TOKEN",
        ):
            cls._env_backup[key] = os.environ.get(key)
        os.environ["ANDREA_SYNC_DB"] = cls._dbpath
        os.environ["ANDREA_SYNC_TELEGRAM_SECRET"] = "testhooksecret"
        os.environ["ANDREA_SYNC_INTERNAL_TOKEN"] = "internal-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = ""

        from services.andrea_sync.server import SyncServer, make_handler

        cls._srv = SyncServer()
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls._srv))
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()
        for key, val in cls._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        Path(cls._dbpath).unlink(missing_ok=True)
        Path(cls._dbpath + ".kill").unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(cls._dbpath + suf).unlink(missing_ok=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def test_health_ok(self) -> None:
        req = urllib.request.Request(self._url("/v1/health"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("kill_switch", data)
        self.assertIn("capability_digest_age_seconds", data)

    def test_status_ok(self) -> None:
        req = urllib.request.Request(self._url("/v1/status"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("kill_switch", data)
        self.assertIn("capabilities", data)
        self.assertIn("runtime", data)

    def test_runtime_snapshot_reports_process_authoritative_truth(self) -> None:
        prev_public = self._srv.telegram_public_base
        prev_token = self._srv.telegram_bot_token
        prev_use_query = self._srv.telegram_use_query_secret
        self._srv.telegram_public_base = "https://runtime.example.test"
        self._srv.telegram_bot_token = "bot-token"
        self._srv.telegram_use_query_secret = True
        try:
            expected_url = self._srv._expected_webhook_url()
            with mock.patch.object(
                tg_adapt,
                "get_webhook_info",
                return_value={"result": {"url": expected_url, "pending_update_count": 0}},
            ):
                req = urllib.request.Request(self._url("/v1/runtime-snapshot"), method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
        finally:
            self._srv.telegram_public_base = prev_public
            self._srv.telegram_bot_token = prev_token
            self._srv.telegram_use_query_secret = prev_use_query
        runtime = data["runtime"]
        self.assertEqual(runtime["source"], "process")
        self.assertEqual(runtime["telegram"]["public_base"], "https://runtime.example.test")
        self.assertEqual(runtime["webhook"]["status"], "healthy")
        self.assertTrue(runtime["webhook"]["required"])
        self.assertIn("capability_digest_status", runtime)

    def test_skill_absence_policy_alias_matches_bluebubbles(self) -> None:
        publish_body = json.dumps(
            {
                "command_type": "PublishCapabilitySnapshot",
                "channel": "internal",
                "payload": {
                    "rows": [
                        {
                            "id": "skill:bluebubbles",
                            "detail": "bluebubbles",
                            "status": "ready",
                            "aliases": ["blue bubbles", "imessage", "text messages"],
                            "availability": "verified_available",
                        }
                    ],
                    "summary": {"ready": 1, "ready_with_limits": 0, "blocked": 0},
                },
            }
        ).encode("utf-8")
        publish_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=publish_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with urllib.request.urlopen(publish_req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        req = urllib.request.Request(
            self._url("/v1/policy/skill-absence?skill=blue%20bubbles"),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertFalse(data["may_claim_absent"])
        self.assertEqual(data["reason"], "verify_before_deny:skill_ready")

    def test_dashboard_html_route(self) -> None:
        req = urllib.request.Request(self._url("/dashboard"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
        self.assertIn("Andrea Monitor", body)
        self.assertIn("/v1/dashboard/summary", body)

    def test_dashboard_summary_includes_projected_tasks(self) -> None:
        create_body = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "telegram",
                "external_id": "dashboard-summary-1",
                "payload": {"summary": "dashboard coverage"},
            }
        ).encode("utf-8")
        create_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create_req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        req = urllib.request.Request(self._url("/v1/dashboard/summary?limit=5"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("webhook", data)
        self.assertIn("capabilities", data)
        self.assertIn("tasks", data)
        self.assertIn("optimization", data)
        self.assertIn("experience_assurance", data)
        self.assertIn("memory", data)
        task_ids = [task["task_id"] for task in data["tasks"]["items"]]
        self.assertIn(created["task_id"], task_ids)

    def test_dashboard_summary_exposes_phase_hints_for_running_delegated_task(self) -> None:
        created = self._srv.with_lock(
            lambda c: handle_command(
                c,
                {
                    "command_type": "CreateTask",
                    "channel": "telegram",
                    "external_id": "dashboard-phase-task",
                    "payload": {"summary": "phase hint coverage"},
                },
            )
        )
        self._srv.with_lock(
            lambda c: append_event(
                c,
                created["task_id"],
                EventType.JOB_QUEUED,
                {
                    "kind": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "collaboration_mode": "cursor_primary",
                },
            )
        )
        self._srv.with_lock(
            lambda c: append_event(
                c,
                created["task_id"],
                EventType.JOB_STARTED,
                {
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "delegated_to_cursor": True,
                    "agent_url": "https://cursor.com/agents/dashboard-phase",
                },
            )
        )
        req = urllib.request.Request(self._url("/v1/dashboard/summary?limit=10"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        item = next(task for task in data["tasks"]["items"] if task["task_id"] == created["task_id"])
        self.assertEqual(item["current_phase"], "execution")
        self.assertEqual(item["current_phase_status"], "running")
        self.assertEqual(item["current_phase_lane"], "cursor")
        self.assertEqual(item["completed_phases"], [])

    def test_dashboard_summary_includes_optimization_loop(self) -> None:
        created = self._srv.with_lock(
            lambda c: handle_command(
                c,
                {
                    "command_type": "CreateTask",
                    "channel": "telegram",
                    "external_id": "dashboard-optimizer-source",
                    "payload": {"summary": "Is this OpenClaw?"},
                },
            )
        )
        self._srv.with_lock(
            lambda c: append_event(
                c,
                created["task_id"],
                EventType.USER_MESSAGE,
                {
                    "text": "Is this OpenClaw?",
                    "routing_text": "Is this OpenClaw?",
                    "channel": "telegram",
                    "chat_id": 4040,
                    "message_id": 91,
                },
            )
        )
        self._srv.with_lock(
            lambda c: append_event(
                c,
                created["task_id"],
                EventType.JOB_QUEUED,
                {
                    "kind": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "route_reason": "stack_or_tooling_question",
                },
            )
        )

        body = json.dumps(
            {
                "command_type": "RunOptimizationCycle",
                "channel": "internal",
                "payload": {
                    "limit": 10,
                    "regression_report": {"passed": True, "total": 8},
                    "emit_proposals": True,
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            result = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(result.get("ok"))

        req_summary = urllib.request.Request(self._url("/v1/dashboard/summary?limit=10"), method="GET")
        with urllib.request.urlopen(req_summary, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["optimization"]["latest_run"]["status"], "completed")
        self.assertTrue(
            any(
                row["category"] == "overdelegation"
                for row in data["optimization"]["dominant_categories"]
            )
        )
        self.assertTrue(
            any(
                row["category"] == "overdelegation"
                for row in data["optimization"]["recent_proposals"]
            )
        )
        self.assertEqual(data["optimization"]["latest_regression"]["total"], 8)

    def test_dashboard_summary_includes_latest_experience_run(self) -> None:
        result = self._srv.with_lock(
            lambda c: run_experience_assurance(
                c,
                actor="http-test",
                repo_path=REPO_ROOT,
            )
        )
        self.assertTrue(result["ok"])
        req = urllib.request.Request(self._url("/v1/dashboard/summary?limit=10"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        latest = data["experience_assurance"]["latest_run"]
        self.assertEqual(latest["run_id"], result["run"]["run_id"])
        self.assertGreaterEqual(latest["total_checks"], 16)
        self.assertIn("runtime", data)
        self.assertGreaterEqual(
            data["experience_assurance"]["delegated_summary"]["total"],
            4,
        )
        self.assertEqual(
            data["experience_assurance"]["delegated_summary"]["failed"],
            0,
        )

    def test_dashboard_summary_projects_incident_conductor_fields(self) -> None:
        def seed(conn) -> None:
            ensure_system_task(conn)
            save_incident(
                conn,
                {
                    "incident_id": "inc_http_conductor",
                    "source_task_id": "",
                    "source": "http_test",
                    "service_name": "andrea_sync",
                    "environment": "local",
                    "error_type": "code_bug",
                    "summary": "Synthetic incident for dashboard conductor projection.",
                    "current_state": "cursor_handoff_ready",
                    "status": "cursor_handoff_ready",
                    "fingerprint": "fp_http_cond",
                    "metadata": {
                        "conductor": {
                            "preferred_executor": "cursor_handoff",
                            "escalation_reasons": ["heavy_repair_plan", "lightweight_attempts_exhausted"],
                            "recommended_cursor_execute": True,
                            "cursor_execute_requested": True,
                            "auto_cursor_heavy": False,
                            "effective_cursor_execute": True,
                            "worktree_clean": True,
                            "metrics": {"plan_files": 3, "plan_steps": 2, "patch_attempts": 2},
                            "handoff": {
                                "ok": True,
                                "branch": "repair/http-demo-branch",
                                "agent_url": "https://cursor.com/agents/http-demo",
                                "pr_url": "",
                            },
                            "outcome": {
                                "submission_status": "succeeded",
                                "verification_status": "skipped",
                                "next_action": "monitor_cursor_or_verify_manually",
                                "terminal_cursor_status": "FINISHED",
                            },
                        },
                    },
                },
            )

        self._srv.with_lock(seed)
        req = urllib.request.Request(self._url("/v1/dashboard/summary?limit=10"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        latest = data["optimization"]["latest_incident"]
        self.assertEqual(latest.get("incident_id"), "inc_http_conductor")
        self.assertEqual(latest.get("conductor_preferred_executor"), "cursor_handoff")
        self.assertEqual(
            latest.get("conductor_reasons"),
            ["heavy_repair_plan", "lightweight_attempts_exhausted"],
        )
        self.assertTrue(latest.get("conductor_effective_cursor_execute"))
        self.assertTrue(latest.get("conductor_worktree_clean"))
        self.assertTrue(latest.get("cursor_handoff_active"))
        self.assertEqual(latest.get("cursor_handoff_branch"), "repair/http-demo-branch")
        self.assertIn("cursor.com", latest.get("cursor_handoff_agent_url") or "")
        self.assertIn("cursor_handoff", latest.get("conductor_summary") or "")
        self.assertEqual(latest.get("conductor_outcome_verification_status"), "skipped")
        self.assertEqual(
            latest.get("conductor_outcome_next_action"),
            "monitor_cursor_or_verify_manually",
        )
        recent = data["optimization"]["recent_incidents"]
        self.assertTrue(recent)
        self.assertEqual(recent[0].get("incident_id"), "inc_http_conductor")

    def test_run_incident_repair_internal_command_requires_auth_and_executes(self) -> None:
        body = json.dumps(
            {
                "command_type": "RunIncidentRepair",
                "channel": "internal",
                "payload": {},
            }
        ).encode("utf-8")
        unauthorized = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(unauthorized, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

        authorized = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with mock.patch(
            "services.andrea_sync.repair_orchestrator.run_incident_repair_cycle",
            return_value={"ok": True, "resolved": False, "incident": {"incident_id": "inc_http"}},
        ):
            with urllib.request.urlopen(authorized, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                payload = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["incident"]["incident_id"], "inc_http")

    def test_run_proactive_sweep_internal_command_delivers_due_reminder(self) -> None:
        create_body = json.dumps(
            {
                "command_type": "CreateReminder",
                "channel": "telegram",
                "task_id": "tsk_system_lockstep",
                "payload": {
                    "principal_id": "prn_http_demo",
                    "message": "drink water",
                    "due_at": time.time() - 5,
                    "delivery_target": "777",
                    "delivery_channel": "telegram",
                },
            }
        ).encode("utf-8")
        create_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create_req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(created.get("ok"))

        sweep_body = json.dumps(
            {
                "command_type": "RunProactiveSweep",
                "channel": "internal",
                "payload": {"limit": 10},
            }
        ).encode("utf-8")
        sweep_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=sweep_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            with urllib.request.urlopen(sweep_req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                result = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(result["delivered"], 1)
        send_mock.assert_called_once()

    def test_command_create_and_fetch_task(self) -> None:
        body = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-test-1",
                "payload": {"summary": "from http test"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            r1 = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(r1.get("ok"))
        tid = r1["task_id"]

        req2 = urllib.request.Request(self._url(f"/v1/tasks/{tid}"), method="GET")
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            self.assertEqual(resp2.status, 200)
            detail = json.loads(resp2.read().decode("utf-8"))
        self.assertIn("task", detail)
        self.assertEqual(detail["task"]["task_id"], tid)

    def test_command_validation_400(self) -> None:
        body = json.dumps({"command_type": "NopeNotACommand", "channel": "cli"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_internal_events_unauthorized(self) -> None:
        body = json.dumps(
            {
                "task_id": "tsk_missing",
                "event_type": "JobCompleted",
                "payload": {},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/internal/events"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_internal_rollout_candidates_requires_auth(self) -> None:
        req = urllib.request.Request(self._url("/v1/internal/rollout/candidates"), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_internal_rollout_approve_live_flow(self) -> None:
        import sqlite3

        os.environ["ANDREA_SYNC_COLLAB_PROMOTION_ENABLED"] = "1"
        try:
            conn = sqlite3.connect(self._dbpath)
            conn.row_factory = sqlite3.Row
            now = time.time()
            for i in range(22):
                conn.execute(
                    """
                    INSERT INTO collaboration_outcomes(
                        task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
                        canonical_class, usefulness_detail, live_advisory_ran, role_invocation_delta,
                        payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"t_http_roll_{i}",
                        "p_http_roll",
                        "s_http_roll",
                        f"c_http_roll_{i}",
                        "repoHelpVerified",
                        "trust_gate",
                        now,
                        "useful",
                        "useful_strategy_shift",
                        0,
                        2,
                        "{}",
                    ),
                )
            conn.commit()
            conn.close()
            body = json.dumps(
                {
                    "action": "approve_live_advisory",
                    "actor": "http_tester",
                    "scenario_id": "repoHelpVerified",
                    "trigger": "trust_gate",
                    "risk_notes": "http test",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/internal/rollout"),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer internal-test-token",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("revision_id", data)
        finally:
            os.environ.pop("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED", None)

    def test_internal_events_rejects_non_object_payload(self) -> None:
        create_body = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-internal-payload",
                "payload": {"summary": "for payload validation"},
            }
        ).encode("utf-8")
        create_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create_req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        body = json.dumps(
            {
                "task_id": created["task_id"],
                "event_type": "JobStarted",
                "payload": ["not", "an", "object"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/internal/events"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_telegram_webhook_forbidden_without_secret(self) -> None:
        body = json.dumps({"update_id": 1, "message": {"text": "hi"}}).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_admin_command_requires_internal_auth(self) -> None:
        body = json.dumps(
            {
                "command_type": "KillSwitchEngage",
                "channel": "internal",
                "payload": {"reason": "x"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_kill_switch_blocks_commands_http(self) -> None:
        auth = {
            "Authorization": "Bearer internal-test-token",
            "Content-Type": "application/json",
        }
        engage = json.dumps(
            {
                "command_type": "KillSwitchEngage",
                "channel": "internal",
                "payload": {"reason": "t"},
            }
        ).encode("utf-8")
        req_e = urllib.request.Request(
            self._url("/v1/commands"), data=engage, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_e, timeout=5) as r:
            self.assertEqual(r.status, 200)
        create = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-ks",
                "payload": {"summary": "blocked"},
            }
        ).encode("utf-8")
        req_c = urllib.request.Request(
            self._url("/v1/commands"), data=create, method="POST", headers=auth
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_c, timeout=5)
        self.assertEqual(ctx.exception.code, 503)
        req_s = urllib.request.Request(self._url("/v1/status"), method="GET")
        with urllib.request.urlopen(req_s, timeout=5) as resp_s:
            self.assertEqual(resp_s.status, 200)
            status_body = json.loads(resp_s.read().decode("utf-8"))
        self.assertTrue(status_body["kill_switch"]["engaged"])
        rel = json.dumps(
            {
                "command_type": "KillSwitchRelease",
                "channel": "internal",
                "payload": {},
            }
        ).encode("utf-8")
        req_r = urllib.request.Request(
            self._url("/v1/commands"), data=rel, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_r, timeout=5) as r:
            self.assertEqual(r.status, 200)
        create_after_release = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-ks-released",
                "payload": {"summary": "allowed"},
            }
        ).encode("utf-8")
        req_ok = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_after_release,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req_ok, timeout=5) as resp_ok:
            self.assertEqual(resp_ok.status, 200)
            data_ok = json.loads(resp_ok.read().decode("utf-8"))
        self.assertTrue(data_ok.get("ok"))

    def test_skill_absence_endpoint_after_publish(self) -> None:
        auth = {
            "Authorization": "Bearer internal-test-token",
            "Content-Type": "application/json",
        }
        pub = json.dumps(
            {
                "command_type": "PublishCapabilitySnapshot",
                "channel": "internal",
                "payload": {
                    "rows": [{"id": "skill:telegram", "status": "ready"}],
                    "summary": {},
                },
            }
        ).encode("utf-8")
        req_p = urllib.request.Request(
            self._url("/v1/commands"), data=pub, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_p, timeout=5) as r:
            self.assertEqual(r.status, 200)
        req_g = urllib.request.Request(
            self._url("/v1/policy/skill-absence?skill=telegram"), method="GET"
        )
        with urllib.request.urlopen(req_g, timeout=5) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertFalse(data.get("may_claim_absent"))

    def test_alexa_direct_request_returns_short_reply_and_completes_task(self) -> None:
        body = json.dumps(
            {
                "session": {"sessionId": "alexa-session-direct"},
                "request": {
                    "type": "IntentRequest",
                    "requestId": "alexa-request-direct",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {"utterance": {"value": "how are you today"}},
                    },
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/alexa"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("ready to help", data["response"]["outputSpeech"]["text"].lower())
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            alexa_tasks = [t for t in tasks if t["channel"] == "alexa"]
            for task in alexa_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("alexa", {}).get("request_id") == "alexa-request-direct":
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")

    def test_alexa_delegate_request_returns_ack_and_queues_task(self) -> None:
        body = json.dumps(
            {
                "session": {"sessionId": "alexa-session-delegate"},
                "request": {
                    "type": "IntentRequest",
                    "requestId": "alexa-request-delegate",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {
                            "utterance": {
                                "value": "please inspect the repo and fix the failing tests"
                            }
                        },
                    },
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/alexa"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        speech = data["response"]["outputSpeech"]["text"].lower()
        self.assertIn("background", speech)
        self.assertNotIn("telegram", speech)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            alexa_tasks = [t for t in tasks if t["channel"] == "alexa"]
            for task in alexa_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("alexa", {}).get("request_id") == "alexa-request-delegate":
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "queued")
        self.assertEqual(detail["task"]["meta"]["execution"]["lane"], "openclaw_hybrid")

    def test_report_cursor_completion_emits_collaboration_events_on_verify_fail(self) -> None:
        prev_collab = os.environ.get("ANDREA_SYNC_COLLABORATION_LAYER")
        prev_strict = os.environ.get("ANDREA_SYNC_STRICT_VERIFICATION")
        os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = "1"
        os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = "1"
        try:
            body = json.dumps(
                {
                    "session": {"sessionId": "alexa-session-collab-smoke"},
                    "request": {
                        "type": "IntentRequest",
                        "requestId": "alexa-request-collab-smoke",
                        "intent": {
                            "name": "AndreaCaptureIntent",
                            "slots": {
                                "utterance": {
                                    "value": "please inspect the repo and fix the failing tests"
                                }
                            },
                        },
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/alexa"),
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)

            detail = None
            for _ in range(80):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=30"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                alexa_tasks = [t for t in tasks if t["channel"] == "alexa"]
                for task in alexa_tasks:
                    req_task = urllib.request.Request(
                        self._url(f"/v1/tasks/{task['task_id']}"), method="GET"
                    )
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        candidate = json.loads(resp_task.read().decode("utf-8"))
                    meta = candidate["task"].get("meta", {})
                    if meta.get("alexa", {}).get("request_id") != "alexa-request-collab-smoke":
                        continue
                    plan_meta = meta.get("plan", {}) if isinstance(meta.get("plan"), dict) else {}
                    if (
                        candidate["task"]["status"] == "queued"
                        and plan_meta.get("plan_id")
                        and plan_meta.get("execute_step_id")
                    ):
                        detail = candidate
                        break
                if detail is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(detail)
            assert detail is not None
            task_id = detail["task"]["task_id"]
            self.assertEqual(detail["task"]["meta"]["plan"]["scenario_id"], "repoHelpVerified")

            started_body = json.dumps(
                {
                    "command_type": "ReportCursorEvent",
                    "channel": "cursor",
                    "task_id": task_id,
                    "payload": {
                        "event_type": "JobStarted",
                        "payload": {
                            "backend": "cursor",
                            "runner": "cursor",
                            "execution_lane": "cursor",
                            "cursor_agent_id": "http-collab-smoke-agent",
                            "status": "STARTED",
                        },
                    },
                }
            ).encode("utf-8")
            started_req = urllib.request.Request(
                self._url("/v1/commands"),
                data=started_body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(started_req, timeout=5) as started_resp:
                self.assertEqual(started_resp.status, 200)

            completed_body = json.dumps(
                {
                    "command_type": "ReportCursorEvent",
                    "channel": "cursor",
                    "task_id": task_id,
                    "payload": {
                        "event_type": "JobCompleted",
                        "payload": {
                            "summary": "reported terminal completion without proof",
                            "backend": "cursor",
                            "runner": "cursor",
                            "execution_lane": "cursor",
                            "cursor_agent_id": "http-collab-smoke-agent",
                            "status": "FINISHED",
                        },
                    },
                }
            ).encode("utf-8")
            completed_req = urllib.request.Request(
                self._url("/v1/commands"),
                data=completed_body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(completed_req, timeout=5) as completed_resp:
                self.assertEqual(completed_resp.status, 200)

            req_task = urllib.request.Request(self._url(f"/v1/tasks/{task_id}"), method="GET")
            with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                final_detail = json.loads(resp_task.read().decode("utf-8"))

            self.assertEqual(final_detail["task"]["status"], "failed")
            event_types = [event["event_type"] for event in final_detail["events"]]
            self.assertIn("VerificationRecorded", event_types)
            self.assertIn("CollaborationRecorded", event_types)
            self.assertIn("JobFailed", event_types)

            verification_event = next(
                event for event in final_detail["events"] if event["event_type"] == "VerificationRecorded"
            )
            self.assertEqual(verification_event["payload"]["verdict"], "fail")
            collab_event = next(
                event for event in final_detail["events"] if event["event_type"] == "CollaborationRecorded"
            )
            self.assertEqual(collab_event["payload"]["trigger"], "verify_fail")
            self.assertTrue(str(collab_event["payload"].get("repair_strategy") or "").strip())

            meta = final_detail["task"]["meta"]
            self.assertEqual(meta["execution"]["verification_state"], "fail")
            self.assertGreaterEqual(int(meta["execution"].get("repair_attempts") or 0), 1)
            self.assertEqual(
                meta["plan"]["repair_state"], collab_event["payload"]["repair_strategy"]
            )
            self.assertEqual(
                meta["collaboration"]["last_repair_strategy"],
                collab_event["payload"]["repair_strategy"],
            )
        finally:
            if prev_collab is None:
                os.environ.pop("ANDREA_SYNC_COLLABORATION_LAYER", None)
            else:
                os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = prev_collab
            if prev_strict is None:
                os.environ.pop("ANDREA_SYNC_STRICT_VERIFICATION", None)
            else:
                os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = prev_strict

    def test_alexa_requires_edge_token_when_configured(self) -> None:
        prev = self._srv.alexa_edge_token
        self._srv.alexa_edge_token = "edge-secret"
        try:
            body = json.dumps(
                {
                    "session": {"sessionId": "alexa-session-auth"},
                    "request": {
                        "type": "IntentRequest",
                        "requestId": "alexa-request-auth",
                        "intent": {
                            "name": "AndreaCaptureIntent",
                            "slots": {"utterance": {"value": "how are you today"}},
                        },
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/alexa"),
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            req_ok = urllib.request.Request(
                self._url("/v1/alexa"),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer edge-secret",
                },
            )
            with urllib.request.urlopen(req_ok, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            self._srv.alexa_edge_token = prev

    def test_alexa_rejects_invalid_json_body(self) -> None:
        req = urllib.request.Request(
            self._url("/v1/alexa"),
            data=b"\xff\xfe\xfd",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "invalid_json")


class TestAndreaSyncHTTPWebhookHeader(unittest.TestCase):
    """Webhook auth via X-Telegram-Bot-Api-Secret-Token only (no query secret)."""

    _httpd: ThreadingHTTPServer
    _srv: object
    _port: int
    _thread: threading.Thread
    _dbpath: str

    @classmethod
    def setUpClass(cls) -> None:
        fd, cls._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._env_backup = {}
        for key in (
            "ANDREA_SYNC_DB",
            "ANDREA_SYNC_TELEGRAM_SECRET",
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET",
            "ANDREA_SYNC_INTERNAL_TOKEN",
            "ANDREA_SYNC_BACKGROUND_ENABLED",
            "ANDREA_SYNC_TELEGRAM_NOTIFIER",
            "TELEGRAM_BOT_TOKEN",
        ):
            cls._env_backup[key] = os.environ.get(key)
        os.environ["ANDREA_SYNC_DB"] = cls._dbpath
        os.environ["ANDREA_SYNC_TELEGRAM_SECRET"] = ""
        os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET"] = "hdrsecret"
        os.environ["ANDREA_SYNC_INTERNAL_TOKEN"] = "internal-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = ""

        from services.andrea_sync.server import SyncServer, make_handler

        cls._srv = SyncServer()
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls._srv))
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()
        for key, val in cls._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        Path(cls._dbpath).unlink(missing_ok=True)
        Path(cls._dbpath + ".kill").unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(cls._dbpath + suf).unlink(missing_ok=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def test_telegram_webhook_accepts_header_secret(self) -> None:
        body = json.dumps(
            {
                "update_id": 42,
                "message": {
                    "text": "please inspect the repo and fix the tests",
                    "message_id": 9,
                    "chat": {"id": 1},
                    "from": {"id": 2},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=5"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            if telegram_tasks:
                tid = telegram_tasks[0]["task_id"]
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{tid}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    detail = json.loads(resp_task.read().decode("utf-8"))
                if detail["task"]["status"] == "queued":
                    break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "queued")
        self.assertEqual(detail["task"]["meta"]["telegram"]["chat_id"], 1)
        self.assertEqual(detail["task"]["meta"]["telegram"]["message_id"], 9)
        self.assertEqual(detail["task"]["meta"]["execution"]["lane"], "openclaw_hybrid")
        self.assertEqual(detail["task"]["meta"]["cursor"]["kind"], "openclaw")

    def test_telegram_webhook_split_prompt_coalesces_one_task(self) -> None:
        prev_cont = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            }
            body1 = json.dumps(
                {
                    "update_id": 9001,
                    "message": {
                        "text": "@Andrea @Cursor collaborate on the spec section A",
                        "message_id": 90001,
                        "chat": {"id": 5050, "type": "private"},
                        "from": {"id": 6060},
                    },
                }
            ).encode("utf-8")
            req1 = urllib.request.Request(
                self._url("/v1/telegram/webhook"),
                data=body1,
                method="POST",
                headers=headers,
            )
            with urllib.request.urlopen(req1, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            body2 = json.dumps(
                {
                    "update_id": 9002,
                    "message": {
                        "text": "Section B: acceptance criteria and rollout.",
                        "message_id": 90002,
                        "chat": {"id": 5050, "type": "private"},
                        "from": {"id": 6060},
                    },
                }
            ).encode("utf-8")
            req2 = urllib.request.Request(
                self._url("/v1/telegram/webhook"),
                data=body2,
                method="POST",
                headers=headers,
            )
            with urllib.request.urlopen(req2, timeout=5) as resp:
                self.assertEqual(resp.status, 200)

            last_detail: dict | None = None
            for _ in range(80):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=50"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                hits = 0
                for row in tasks:
                    if row.get("channel") != "telegram":
                        continue
                    tid = row["task_id"]
                    req_task = urllib.request.Request(
                        self._url(f"/v1/tasks/{tid}"), method="GET"
                    )
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        detail = json.loads(resp_task.read().decode("utf-8"))
                    tg = detail["task"].get("meta", {}).get("telegram", {})
                    if tg.get("chat_id") == 5050:
                        hits += 1
                        last_detail = detail
                if hits == 1 and last_detail is not None:
                    tg = last_detail["task"]["meta"]["telegram"]
                    if tg.get("continuation_count") == 1:
                        acc = str(tg.get("accumulated_prompt") or "")
                        self.assertIn("section a", acc.lower())
                        self.assertIn("section b", acc.lower())
                        break
                time.sleep(0.05)
            else:
                self.fail("expected one coalesced telegram task for chat 5050 with continuation_count=1")
            self.assertEqual(hits, 1)
        finally:
            if prev_cont is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev_cont

    def test_telegram_greeting_routes_direct_without_cursor_task(self) -> None:
        body = json.dumps(
            {
                "update_id": 43,
                "message": {
                    "text": "hi andrea how are you?",
                    "message_id": 10,
                    "chat": {"id": 2},
                    "from": {"id": 3},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=10"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 10:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", detail["task"]["meta"])

    def test_telegram_history_followups_do_not_recycle_latest_useful_thread(self) -> None:
        prev_enabled = os.environ.get("OPENAI_API_ENABLED")
        prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)

        def submit(update_id: int, message_id: int, text: str) -> None:
            body = json.dumps(
                {
                    "update_id": update_id,
                    "message": {
                        "text": text,
                        "message_id": message_id,
                        "chat": {"id": 21},
                        "from": {"id": 22},
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/telegram/webhook"),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)

        def wait_for_detail(message_id: int) -> dict:
            detail = None
            for _ in range(40):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
                for task in telegram_tasks:
                    req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        candidate = json.loads(resp_task.read().decode("utf-8"))
                    meta = candidate["task"].get("meta", {})
                    if meta.get("telegram", {}).get("message_id") == message_id:
                        detail = candidate
                        break
                if detail and detail["task"]["status"] == "completed":
                    return detail
                time.sleep(0.05)
            self.assertIsNotNone(detail)
            assert detail is not None
            return detail

        try:
            with (
                mock.patch.object(
                    self._srv,
                    "_resolve_runtime_skill",
                    return_value={"skill_key": "brave-api-search", "truth": {"status": "verified_available"}},
                ) as resolve_mock,
                mock.patch.object(
                    self._srv,
                    "_create_openclaw_job",
                    return_value={
                        "ok": True,
                        "user_summary": "Live news: AI and market headlines led the day, with policy updates still moving.",
                    },
                ) as job_mock,
            ):
                submit(60, 60, "Hi @andrea what's the news today?")
                first = wait_for_detail(60)
                submit(61, 61, "What's the news today?")
                second = wait_for_detail(61)
                submit(62, 62, "OpenClaw are you there?")
                third = wait_for_detail(62)
            self.assertEqual(resolve_mock.call_count, 2)
            self.assertEqual(job_mock.call_count, 2)
        finally:
            if prev_enabled is None:
                os.environ.pop("OPENAI_API_ENABLED", None)
            else:
                os.environ["OPENAI_API_ENABLED"] = prev_enabled
            if prev_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_key

        first_reply = first["task"]["meta"]["assistant"]["last_reply"].lower()
        second_reply = second["task"]["meta"]["assistant"]["last_reply"].lower()
        third_reply = third["task"]["meta"]["assistant"]["last_reply"].lower()
        recycled_terms = (
            "latest useful thread",
            "recent context from this chat",
            "latest useful context",
            "recent thread:",
        )
        self.assertIn("news", first_reply)
        self.assertNotIn("what would you like to do", first_reply)
        self.assertEqual(first["task"]["meta"]["assistant"]["route"], "direct")
        self.assertEqual(first["task"]["meta"]["assistant"]["reason"], "news_summary_ready")
        self.assertNotIn("cursor", first["task"]["meta"])
        self.assertNotIn("JobQueued", [row["event_type"] for row in first["events"]])
        self.assertIn("news", second_reply)
        self.assertEqual(second["task"]["meta"]["assistant"]["route"], "direct")
        self.assertEqual(second["task"]["meta"]["assistant"]["reason"], "news_summary_ready")
        for term in recycled_terms:
            self.assertNotIn(term, second_reply)
        self.assertNotIn("JobQueued", [row["event_type"] for row in second["events"]])
        self.assertIn("openclaw", third_reply)
        self.assertIn("andrea", third_reply)
        self.assertEqual(third["task"]["meta"]["assistant"]["route"], "direct")
        for term in recycled_terms:
            self.assertNotIn(term, third_reply)
        self.assertNotIn("JobQueued", [row["event_type"] for row in third["events"]])

    def test_telegram_recent_text_messages_use_bluebubbles_lane(self) -> None:
        body = json.dumps(
            {
                "update_id": 63,
                "message": {
                    "text": "@andrea what are my recent text messages?",
                    "message_id": 63,
                    "chat": {"id": 31},
                    "from": {"id": 32},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with (
            mock.patch.object(
                self._srv,
                "_resolve_messaging_capability",
                return_value={
                    "skill_key": "bluebubbles",
                    "label": "text messaging",
                    "truth": {"status": "verified_available"},
                },
            ) as resolve_mock,
            mock.patch.object(
                self._srv,
                "_create_openclaw_job",
                return_value={
                    "ok": True,
                    "user_summary": "Recent texts: Candace said she's on her way, and Michael asked whether tomorrow still works.",
                },
            ) as job_mock,
        ):
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            detail = None
            for _ in range(40):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
                for task in telegram_tasks:
                    req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        candidate = json.loads(resp_task.read().decode("utf-8"))
                    meta = candidate["task"].get("meta", {})
                    if meta.get("telegram", {}).get("message_id") == 63:
                        detail = candidate
                        break
                if detail and detail["task"]["status"] == "completed":
                    break
                time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        reply = detail["task"]["meta"]["assistant"]["last_reply"].lower()
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertEqual(detail["task"]["meta"]["assistant"]["reason"], "recent_text_messages_ready")
        self.assertIn("recent texts", reply)
        self.assertNotIn("session", reply)
        self.assertNotIn("cursor", detail["task"]["meta"])
        self.assertNotIn("JobQueued", [row["event_type"] for row in detail["events"]])
        resolve_mock.assert_called_once()
        job_mock.assert_called_once()

    def test_telegram_is_this_openclaw_routes_direct(self) -> None:
        """Success gate: Is this OpenClaw? stays direct, no delegation lifecycle."""
        body = json.dumps(
            {
                "update_id": 50,
                "message": {
                    "text": "Is this OpenClaw?",
                    "message_id": 11,
                    "chat": {"id": 3},
                    "from": {"id": 4},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=10"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 11:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", detail["task"]["meta"])
        reply = detail["task"]["meta"]["assistant"]["last_reply"].lower()
        self.assertIn("openclaw", reply)
        self.assertIn("andrea", reply)
        self.assertIn("collaboration layer", reply)

    def test_telegram_what_is_cursor_routes_direct(self) -> None:
        """Success gate: What is Cursor? stays direct, no delegation lifecycle."""
        body = json.dumps(
            {
                "update_id": 51,
                "message": {
                    "text": "What is Cursor?",
                    "message_id": 12,
                    "chat": {"id": 4},
                    "from": {"id": 5},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=10"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 12:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", detail["task"]["meta"])
        reply = detail["task"]["meta"]["assistant"]["last_reply"].lower()
        self.assertIn("cursor", reply)
        self.assertIn("andrea", reply)
        self.assertIn("execution lane", reply)

    def test_telegram_what_llm_is_answering_routes_direct(self) -> None:
        """Success gate: What LLM is answering? stays direct, no delegation lifecycle."""
        body = json.dumps(
            {
                "update_id": 52,
                "message": {
                    "text": "What LLM is answering?",
                    "message_id": 13,
                    "chat": {"id": 5},
                    "from": {"id": 6},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=10"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 13:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", detail["task"]["meta"])
        reply = detail["task"]["meta"]["assistant"]["last_reply"].lower()
        self.assertIn("andrea", reply)
        self.assertIn("directly", reply)
        self.assertNotIn("execution lane", reply)

    def test_telegram_memory_question_uses_prior_chat_context(self) -> None:
        prev_enabled = os.environ.get("OPENAI_API_ENABLED")
        prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            prior = self._srv.with_lock(
                lambda c: handle_command(
                    c,
                    {
                        "command_type": "SubmitUserMessage",
                        "channel": "telegram",
                        "external_id": "http-memory-prior",
                        "payload": {
                            "text": "Let's finish the reboot startup work.",
                            "chat_id": 22,
                            "message_id": 30,
                        },
                    },
                )
            )
            self._srv.with_lock(
                lambda c: append_event(
                    c,
                    prior["task_id"],
                    EventType.ASSISTANT_REPLIED,
                    {
                        "text": "We were working on reboot startup and Telegram memory.",
                        "route": "direct",
                        "reason": "history",
                    },
                )
            )
            body = json.dumps(
                {
                    "update_id": 44,
                    "message": {
                        "text": "Hi do you remember before?",
                        "message_id": 31,
                        "chat": {"id": 22},
                        "from": {"id": 3},
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/telegram/webhook"),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            detail = None
            for _ in range(40):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
                for task in telegram_tasks:
                    req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        candidate = json.loads(resp_task.read().decode("utf-8"))
                    meta = candidate["task"].get("meta", {})
                    if meta.get("telegram", {}).get("message_id") == 31:
                        detail = candidate
                        break
                if detail and detail["task"]["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertIn("remember the recent conversation", detail["task"]["meta"]["assistant"]["last_reply"].lower())
            self.assertIn("reboot startup", detail["task"]["meta"]["assistant"]["last_reply"].lower())
        finally:
            if prev_enabled is None:
                os.environ.pop("OPENAI_API_ENABLED", None)
            else:
                os.environ["OPENAI_API_ENABLED"] = prev_enabled
            if prev_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_key

    def test_telegram_final_message_clips_long_cursor_excerpt(self) -> None:
        long_summary = "Implemented result. " + ("detail " * 300)
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary=long_summary,
            agent_url="https://cursor.com/agents/demo",
        )
        self.assertIn("Cursor said:", text)
        self.assertIn("Technical details:", text)
        self.assertLess(len(text), 1600)

    def test_telegram_final_message_for_openclaw_only_lane(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Created a reminder and captured the note.",
            worker_label="OpenClaw",
            openclaw_session_id="sess-demo",
        )
        self.assertNotIn("OpenClaw said:", text)
        self.assertNotIn("OpenClaw session:", text)

    def test_telegram_cursor_mention_sets_cursor_primary_routing(self) -> None:
        body = json.dumps(
            {
                "update_id": 45,
                "message": {
                    "text": "@Cursor please fix the failing tests",
                    "message_id": 33,
                    "chat": {"id": 23},
                    "from": {"id": 3},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 33:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["meta"]["telegram"]["routing_hint"], "cursor")
        self.assertEqual(detail["task"]["meta"]["execution"]["collaboration_mode"], "cursor_primary")
        self.assertEqual(detail["task"]["meta"]["telegram"]["requested_capability"], "cursor_execution")
        self.assertEqual(detail["task"]["task_id"], detail["task"]["task_id"])

    def test_telegram_collaboration_request_sets_full_visibility_mode(self) -> None:
        body = json.dumps(
            {
                "update_id": 46,
                "message": {
                    "text": "@Andrea @Cursor work together and show the full dialogue while you do it",
                    "message_id": 34,
                    "chat": {"id": 24},
                    "from": {"id": 4},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 34:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["meta"]["telegram"]["visibility_mode"], "full")
        self.assertEqual(detail["task"]["meta"]["execution"]["visibility_mode"], "full")
        self.assertEqual(detail["task"]["meta"]["telegram"]["requested_capability"], "collaboration")

    def test_telegram_model_mention_sets_preferred_lane(self) -> None:
        body = json.dumps(
            {
                "update_id": 47,
                "message": {
                    "text": "@Gemini review this repo plan",
                    "message_id": 35,
                    "chat": {"id": 25},
                    "from": {"id": 5},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 35:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["meta"]["telegram"]["preferred_model_family"], "gemini")
        self.assertEqual(detail["task"]["meta"]["execution"]["preferred_model_label"], "Gemini")


if __name__ == "__main__":
    unittest.main()
