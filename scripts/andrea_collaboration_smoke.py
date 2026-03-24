#!/usr/bin/env python3
"""
Drive a bounded live collaboration smoke against andrea_sync.

Flow:
1. Create a CLI task over HTTP so the running server owns the task lifecycle.
2. Seed a repoHelpVerified delegated plan directly in the live DB using real plan runtime helpers.
3. Append SCENARIO_RESOLVED and JOB_QUEUED through HTTP.
4. Report a terminal cursor completion over HTTP with no PR/proof.
5. Assert the task detail shows VerificationRecorded, CollaborationRecorded, and repair metadata.

Requires:
  ANDREA_SYNC_INTERNAL_TOKEN
Optional:
  ANDREA_SYNC_URL                default http://127.0.0.1:8765
  ANDREA_COLLAB_SMOKE_PROMPT     default "please inspect the repo and fix the failing tests"
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.andrea_router import route_message  # noqa: E402
from services.andrea_sync.plan_runtime import gate_delegated_job  # noqa: E402
from services.andrea_sync.scenario_runtime import (  # noqa: E402
    resolve_scenario,
    scenario_job_payload_fields,
)


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def http_json(
    method: str,
    url: str,
    *,
    payload: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    data = None
    merged_headers = {"Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=merged_headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def status_and_db(base_url: str) -> Tuple[Dict[str, Any], str]:
    status = http_json("GET", f"{base_url}/v1/status", payload=None, headers={})
    db_path = str(status.get("db") or "").strip()
    if not db_path:
        raise RuntimeError("status response did not include db path")
    return status, db_path


def seed_plan(db_path: str, task_id: str, request_text: str) -> Dict[str, Any]:
    route = route_message(request_text)
    resolution, contract = resolve_scenario(request_text, route_decision=route)
    execution_lane = (
        str(route.delegate_target or "")
        or str(resolution.suggested_lane or "")
        or "openclaw_hybrid"
    )
    runner = "openclaw" if execution_lane == "openclaw_hybrid" else "cursor"
    job_payload: Dict[str, Any] = {
        "kind": runner,
        "runner": runner,
        "source": "collaboration_smoke_seed",
        "route_reason": str(route.reason or "collaboration_smoke_seed"),
        "execution_lane": execution_lane,
        "routing_hint": "collaboration_smoke",
        "collaboration_mode": str(route.collaboration_mode or "cursor_primary"),
        "visibility_mode": "summary",
    }
    job_payload.update(scenario_job_payload_fields(resolution, contract))

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        gate = gate_delegated_job(
            conn,
            task_id,
            "",
            "",
            request_text[:200],
            execution_lane,
            dict(job_payload),
            [],
        )
        conn.commit()
    finally:
        conn.close()

    if gate.mode != "proceed":
        raise RuntimeError(f"expected gate mode proceed, got {gate.mode}")
    if not gate.plan_id or not gate.execute_step_id:
        raise RuntimeError("plan gate did not return plan_id and execute_step_id")

    return {
        "resolution": resolution.to_event_payload(),
        "job_payload": {
            **job_payload,
            "plan_id": gate.plan_id,
            "execute_step_id": gate.execute_step_id,
        },
        "plan_id": gate.plan_id,
        "execute_step_id": gate.execute_step_id,
        "scenario_id": resolution.scenario_id,
        "execution_lane": execution_lane,
    }


def find_event(detail: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    for event in detail.get("events") or []:
        if str(event.get("event_type") or "") == event_type:
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    raise RuntimeError(f"missing event {event_type}")


def main() -> int:
    token = (os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN") or "").strip()
    if not token:
        return fail("ANDREA_SYNC_INTERNAL_TOKEN required")
    base_url = (os.environ.get("ANDREA_SYNC_URL") or "http://127.0.0.1:8765").rstrip("/")
    request_text = (
        os.environ.get("ANDREA_COLLAB_SMOKE_PROMPT")
        or "please inspect the repo and fix the failing tests"
    ).strip()
    auth = {"Authorization": f"Bearer {token}"}

    try:
        status, db_path = status_and_db(base_url)
        if status.get("ok") is not True:
            return fail("/v1/status not ok")

        create = http_json(
            "POST",
            f"{base_url}/v1/commands",
            payload={
                "command_type": "SubmitUserMessage",
                "channel": "cli",
                "external_id": f"collab-smoke-{uuid.uuid4().hex[:10]}",
                "payload": {
                    "text": request_text,
                    "routing_text": request_text,
                },
            },
        )
        task_id = str(create.get("task_id") or "").strip()
        if create.get("ok") is not True or not task_id:
            return fail(f"task creation failed: {create}")

        seeded = seed_plan(db_path, task_id, request_text)
        if seeded["scenario_id"] != "repoHelpVerified":
            return fail(f"expected repoHelpVerified scenario, got {seeded['scenario_id']}")

        http_json(
            "POST",
            f"{base_url}/v1/internal/events",
            payload={
                "task_id": task_id,
                "event_type": "ScenarioResolved",
                "payload": seeded["resolution"],
            },
            headers=auth,
        )
        http_json(
            "POST",
            f"{base_url}/v1/internal/events",
            payload={
                "task_id": task_id,
                "event_type": "JobQueued",
                "payload": seeded["job_payload"],
            },
            headers=auth,
        )
        http_json(
            "POST",
            f"{base_url}/v1/commands",
            payload={
                "command_type": "ReportCursorEvent",
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": "JobStarted",
                    "payload": {
                        "backend": "cursor",
                        "runner": "cursor",
                        "execution_lane": seeded["execution_lane"],
                        "cursor_agent_id": "live-collab-smoke-agent",
                        "status": "STARTED",
                    },
                },
            },
        )
        http_json(
            "POST",
            f"{base_url}/v1/commands",
            payload={
                "command_type": "ReportCursorEvent",
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": "JobCompleted",
                    "payload": {
                        "summary": "live collaboration smoke terminal completion without PR proof",
                        "backend": "cursor",
                        "runner": "cursor",
                        "execution_lane": seeded["execution_lane"],
                        "cursor_agent_id": "live-collab-smoke-agent",
                        "status": "FINISHED",
                    },
                },
            },
        )

        time.sleep(0.2)
        detail = http_json(
            "GET",
            f"{base_url}/v1/tasks/{urllib.parse.quote(task_id)}",
            payload=None,
            headers={},
        )
        task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
        meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
        event_types = [str(event.get("event_type") or "") for event in detail.get("events") or []]
        if task.get("status") != "failed":
            return fail(f"expected failed task after verification block, got {task.get('status')}")
        for required in ("VerificationRecorded", "CollaborationRecorded", "JobFailed"):
            if required not in event_types:
                return fail(f"missing required event {required}")

        verification_payload = find_event(detail, "VerificationRecorded")
        collaboration_payload = find_event(detail, "CollaborationRecorded")
        repair_strategy = str(collaboration_payload.get("repair_strategy") or "").strip()
        if verification_payload.get("verdict") != "fail":
            return fail(f"expected verification verdict fail, got {verification_payload}")
        if not repair_strategy:
            return fail(f"missing collaboration repair strategy: {collaboration_payload}")
        if str((meta.get("plan") or {}).get("scenario_id") or "") != "repoHelpVerified":
            return fail(f"missing repoHelpVerified projection metadata: {meta}")
        if str((meta.get("plan") or {}).get("repair_state") or "") != repair_strategy:
            return fail(f"projection repair_state mismatch: {meta}")
        if int((meta.get("execution") or {}).get("repair_attempts") or 0) < 1:
            return fail(f"missing repair attempt count: {meta}")

        print(
            json.dumps(
                {
                    "ok": True,
                    "task_id": task_id,
                    "scenario_id": seeded["scenario_id"],
                    "plan_id": seeded["plan_id"],
                    "execute_step_id": seeded["execute_step_id"],
                    "required_events": [
                        event_type
                        for event_type in event_types
                        if event_type in {"VerificationRecorded", "CollaborationRecorded", "JobFailed"}
                    ],
                    "task_status": task.get("status"),
                    "verification_verdict": verification_payload.get("verdict"),
                    "collab_id": collaboration_payload.get("collab_id"),
                    "collaboration_trigger": collaboration_payload.get("trigger"),
                    "repair_strategy": repair_strategy,
                    "arbitration_decision": collaboration_payload.get("arbitration_decision"),
                    "repair_state": (meta.get("plan") or {}).get("repair_state"),
                    "arbitration_state": (meta.get("plan") or {}).get("arbitration_state"),
                    "repair_attempts": (meta.get("execution") or {}).get("repair_attempts"),
                },
                indent=2,
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return fail(f"http {exc.code}: {body}")
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
