"""Domain-specific bounded repair outcomes for low-risk daily assistant flows (Stage A)."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import sqlite3

from .assistant_domain_rollout import TRUSTED_DAILY_ASSISTANT_PACK_ID
from .schema import EventType


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _ensure_system_task(conn: sqlite3.Connection) -> None:
    from .store import SYSTEM_TASK_ID, create_task, task_exists

    if not task_exists(conn, SYSTEM_TASK_ID):
        create_task(conn, SYSTEM_TASK_ID, "internal")


def record_domain_repair_outcome(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    scenario_id: str,
    repair_family: str,
    result: str,
    executed: bool = False,
    fallback_used: bool = False,
    trust_safe: bool = True,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist repair ledger row, append task event, mirror on system timeline for operators."""
    from .store import append_event, insert_domain_repair_outcome_row

    oid = _new_id("drep")
    domain_id = TRUSTED_DAILY_ASSISTANT_PACK_ID
    insert_domain_repair_outcome_row(
        conn,
        repair_outcome_id=oid,
        domain_id=domain_id,
        scenario_id=str(scenario_id or "")[:120],
        task_id=str(task_id or ""),
        repair_family=str(repair_family or "")[:120],
        executed=executed,
        result=str(result or "")[:2000],
        fallback_used=fallback_used,
        trust_safe=trust_safe,
        payload=payload or {},
    )
    ep = {
        "repair_outcome_id": oid,
        "domain_id": domain_id,
        "scenario_id": str(scenario_id or ""),
        "task_id": str(task_id or ""),
        "repair_family": str(repair_family or ""),
        "executed": bool(executed),
        "result": str(result or "")[:500],
        "fallback_used": bool(fallback_used),
        "trust_safe": bool(trust_safe),
    }
    append_event(conn, str(task_id or ""), EventType.DOMAIN_REPAIR_OUTCOME_RECORDED, ep)
    _ensure_system_task(conn)
    from .store import SYSTEM_TASK_ID as _SYS

    append_event(conn, _SYS, EventType.DOMAIN_REPAIR_OUTCOME_RECORDED, ep)
    return {"ok": True, "repair_outcome_id": oid}


def suggest_missing_reminder_target_repair(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reminder_id: str,
    principal_id: str = "",
) -> Dict[str, Any]:
    """Observability-only suggestion when reminders lack a delivery target (no auto-send)."""
    return record_domain_repair_outcome(
        conn,
        task_id=task_id,
        scenario_id="noteOrReminderCapture",
        repair_family="resolve_missing_reminder_target",
        result="Reminder saved; operator/user should confirm delivery channel (e.g. Telegram chat).",
        executed=False,
        fallback_used=False,
        trust_safe=True,
        payload={"reminder_id": str(reminder_id or ""), "principal_id": str(principal_id or "")},
    )
