"""Bounded Cursor control-plane helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TERMINAL_CURSOR_STATUSES = frozenset({"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"})


@dataclass(frozen=True)
class CursorControlItemResult:
    id: str
    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class CursorControlResult:
    action: str
    requested_count: int = 0
    terminal_already_count: int = 0
    canceled_count: int = 0
    failed_count: int = 0
    active_count: int = 0
    results: tuple[CursorControlItemResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "requested_count": self.requested_count,
            "terminal_already_count": self.terminal_already_count,
            "canceled_count": self.canceled_count,
            "failed_count": self.failed_count,
            "active_count": self.active_count,
            "results": [item.to_dict() for item in self.results],
        }


def _run_json_command(repo_root: Path, *args: str, timeout_seconds: int = 120) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str((repo_root / "scripts" / "cursor_openclaw.py").resolve()),
        "--json",
        *[str(arg) for arg in args],
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=False,
        timeout=max(5, int(timeout_seconds)),
    )
    stdout = (proc.stdout or "").strip()
    try:
        outer = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        outer = {"raw": stdout, "stderr": proc.stderr}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "outer": outer,
        "response": outer.get("response") if isinstance(outer, dict) else outer,
        "stderr": proc.stderr or "",
    }


def _extract_agents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response")
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        if isinstance(response.get("agents"), list):
            return [item for item in response["agents"] if isinstance(item, dict)]
        if isinstance(response.get("items"), list):
            return [item for item in response["items"] if isinstance(item, dict)]
    outer = payload.get("outer")
    if isinstance(outer, dict):
        if isinstance(outer.get("agents"), list):
            return [item for item in outer["agents"] if isinstance(item, dict)]
        response = outer.get("response")
        if isinstance(response, dict) and isinstance(response.get("agents"), list):
            return [item for item in response["agents"] if isinstance(item, dict)]
    return []


def _agent_id(agent: dict[str, Any]) -> str:
    return str(agent.get("id") or agent.get("agentId") or "").strip()


def _agent_status(agent: dict[str, Any]) -> str:
    return str(agent.get("status") or "").strip().upper()


def _is_terminal(status: str) -> bool:
    return str(status or "").strip().upper() in TERMINAL_CURSOR_STATUSES


def list_jobs(*, repo_root: Path, limit: int = 100) -> list[dict[str, Any]]:
    payload = _run_json_command(repo_root, "list-agents", "--limit", str(max(1, int(limit))))
    return _extract_agents(payload)


def list_active_jobs(*, repo_root: Path, limit: int = 100) -> CursorControlResult:
    agents = list_jobs(repo_root=repo_root, limit=limit)
    active = []
    for agent in agents:
        aid = _agent_id(agent)
        status = _agent_status(agent)
        if not aid or _is_terminal(status):
            continue
        active.append(CursorControlItemResult(id=aid, status=status.lower() or "running"))
    return CursorControlResult(
        action="list_active_jobs",
        requested_count=len(agents),
        active_count=len(active),
        results=tuple(active),
    )


def list_all_jobs(*, repo_root: Path, limit: int = 100) -> CursorControlResult:
    agents = list_jobs(repo_root=repo_root, limit=limit)
    results = tuple(
        CursorControlItemResult(
            id=_agent_id(agent),
            status=(_agent_status(agent) or "unknown").lower(),
        )
        for agent in agents
        if _agent_id(agent)
    )
    active_count = sum(1 for item in results if not _is_terminal(item.status))
    return CursorControlResult(
        action="list_jobs",
        requested_count=len(results),
        active_count=active_count,
        terminal_already_count=len(results) - active_count,
        results=results,
    )


def cancel_all_jobs(*, repo_root: Path, limit: int = 100) -> CursorControlResult:
    agents = list_jobs(repo_root=repo_root, limit=limit)
    results: list[CursorControlItemResult] = []
    terminal_already_count = 0
    canceled_count = 0
    failed_count = 0

    for agent in agents:
        aid = _agent_id(agent)
        status = _agent_status(agent)
        if not aid:
            continue
        if _is_terminal(status):
            terminal_already_count += 1
            results.append(CursorControlItemResult(id=aid, status="already_finished"))
            continue
        stop = _run_json_command(repo_root, "stop-agent", "--id", aid)
        if stop.get("ok"):
            canceled_count += 1
            results.append(CursorControlItemResult(id=aid, status="canceled"))
            continue
        failed_count += 1
        results.append(
            CursorControlItemResult(
                id=aid,
                status="cancel_failed",
                reason=str(stop.get("stderr") or stop.get("outer") or "stop failed"),
            )
        )

    return CursorControlResult(
        action="cancel_jobs",
        requested_count=len(results),
        terminal_already_count=terminal_already_count,
        canceled_count=canceled_count,
        failed_count=failed_count,
        active_count=max(0, len(results) - terminal_already_count),
        results=tuple(results),
    )
