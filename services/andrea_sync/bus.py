"""Command bus: validate commands, enforce idempotency, append events."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from .kill_switch import engage_kill_switch, is_kill_switch_engaged, release_kill_switch
from .policy import META_DIGEST_KEY, META_DIGEST_TS_KEY
from .schema import (
    Channel,
    CommandEnvelope,
    CommandType,
    EventType,
    new_task_id,
    normalize_scoped_idempotency_key,
    validate_command_type,
)
from .store import (
    SYSTEM_TASK_ID,
    append_event,
    append_goal_event,
    claim_idempotency_and_create_task,
    claim_scoped_idempotency,
    create_goal,
    create_reminder,
    create_workflow,
    ensure_system_task,
    get_goal,
    get_principal_recent_telegram_chat_id,
    get_task_channel,
    get_task_principal_id,
    get_workflow,
    link_principal_identity,
    link_task_to_goal,
    link_task_principal,
    list_due_reminders,
    list_tasks_for_goal,
    record_goal_artifact,
    resolve_principal_id,
    save_principal_memory,
    set_meta,
    set_principal_preference,
    task_exists,
    update_goal_status,
    update_reminder,
    update_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_ADMIN_COMMAND_TYPES: Set[CommandType] = {
    CommandType.PUBLISH_CAPABILITY_SNAPSHOT,
    CommandType.HEAL_RUNTIME_CAPABILITY,
    CommandType.RECORD_EVALUATION_FINDING,
    CommandType.CREATE_OPTIMIZATION_PROPOSAL,
    CommandType.RUN_OPTIMIZATION_CYCLE,
    CommandType.APPLY_OPTIMIZATION_PROPOSAL,
    CommandType.RUN_INCIDENT_REPAIR,
    CommandType.LINK_PRINCIPAL_IDENTITY,
    CommandType.RUN_PROACTIVE_SWEEP,
    CommandType.KILL_SWITCH_ENGAGE,
    CommandType.KILL_SWITCH_RELEASE,
    CommandType.CREATE_GOAL,
    CommandType.LINK_TASK_TO_GOAL,
    CommandType.UPDATE_GOAL_STATUS,
    CommandType.RECORD_GOAL_ARTIFACT,
    CommandType.CREATE_WORKFLOW,
    CommandType.ADVANCE_WORKFLOW,
    CommandType.RESOLVE_GOAL_APPROVAL,
    CommandType.RECORD_VERIFICATION_RESULT,
}


def handle_command(conn: sqlite3.Connection, body: Dict[str, Any]) -> Dict[str, Any]:
    """Process one command envelope. Returns JSON-serializable result."""
    ct_raw = body.get("command_type") or body.get("type")
    if not ct_raw:
        return {"ok": False, "error": "missing command_type"}
    try:
        ctype = validate_command_type(str(ct_raw))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ch_raw = body.get("channel") or "cli"
    try:
        channel = Channel(str(ch_raw))
    except ValueError:
        return {"ok": False, "error": f"unknown channel: {ch_raw}"}
    if ctype in _ADMIN_COMMAND_TYPES and channel != Channel.INTERNAL:
        return {"ok": False, "error": "admin commands require channel=internal"}
    if is_kill_switch_engaged(conn) and ctype not in (CommandType.KILL_SWITCH_RELEASE,):
        return {"ok": False, "error": "kill_switch_engaged"}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    task_id_in = body.get("task_id")
    ext_id = body.get("external_id")
    idem_in = body.get("idempotency_key")

    env = CommandEnvelope(
        command_type=ctype,
        channel=channel,
        payload=payload,
        task_id=str(task_id_in) if task_id_in else None,
        idempotency_key=str(idem_in) if idem_in else None,
        external_id=str(ext_id) if ext_id else None,
    )
    idem = env.resolved_idempotency_key()

    if ctype == CommandType.REPORT_CURSOR_EVENT:
        return _handle_cursor_report(conn, env, idem)
    if ctype == CommandType.CREATE_CURSOR_JOB:
        return _handle_create_cursor_job(conn, env, idem)
    if ctype == CommandType.CURSOR_FOLLOWUP:
        return _handle_cursor_followup(conn, env)
    if ctype == CommandType.CURSOR_STOP:
        return _handle_cursor_stop(conn, env)
    if ctype == CommandType.SUBMIT_USER_MESSAGE:
        return _handle_user_message(conn, env, idem)
    if ctype == CommandType.SUBMIT_USER_FEEDBACK:
        return _handle_user_feedback(conn, env)
    if ctype == CommandType.CREATE_TASK:
        return _handle_create_task(conn, env, idem)
    if ctype == CommandType.ALEXA_UTTERANCE:
        return _handle_alexa(conn, env, idem)
    if ctype == CommandType.PUBLISH_CAPABILITY_SNAPSHOT:
        return _handle_publish_capability_snapshot(conn, env)
    if ctype == CommandType.HEAL_RUNTIME_CAPABILITY:
        return _handle_heal_runtime_capability(conn, env)
    if ctype == CommandType.RECORD_EVALUATION_FINDING:
        return _handle_record_evaluation_finding(conn, env)
    if ctype == CommandType.CREATE_OPTIMIZATION_PROPOSAL:
        return _handle_create_optimization_proposal(conn, env)
    if ctype == CommandType.RUN_OPTIMIZATION_CYCLE:
        return _handle_run_optimization_cycle(conn, env)
    if ctype == CommandType.APPLY_OPTIMIZATION_PROPOSAL:
        return _handle_apply_optimization_proposal(conn, env)
    if ctype == CommandType.RUN_INCIDENT_REPAIR:
        return _handle_run_incident_repair(conn, env)
    if ctype == CommandType.SAVE_PRINCIPAL_MEMORY:
        return _handle_save_principal_memory(conn, env)
    if ctype == CommandType.SET_PRINCIPAL_PREFERENCE:
        return _handle_set_principal_preference(conn, env)
    if ctype == CommandType.LINK_PRINCIPAL_IDENTITY:
        return _handle_link_principal_identity(conn, env)
    if ctype == CommandType.CREATE_REMINDER:
        return _handle_create_reminder(conn, env)
    if ctype == CommandType.RUN_PROACTIVE_SWEEP:
        return _handle_run_proactive_sweep(conn, env)
    if ctype == CommandType.KILL_SWITCH_ENGAGE:
        return _handle_kill_switch_engage(conn, env)
    if ctype == CommandType.KILL_SWITCH_RELEASE:
        return _handle_kill_switch_release(conn, env)
    if ctype == CommandType.CREATE_GOAL:
        return _handle_create_goal(conn, env)
    if ctype == CommandType.LINK_TASK_TO_GOAL:
        return _handle_link_task_to_goal(conn, env)
    if ctype == CommandType.UPDATE_GOAL_STATUS:
        return _handle_update_goal_status(conn, env)
    if ctype == CommandType.RECORD_GOAL_ARTIFACT:
        return _handle_record_goal_artifact(conn, env)
    if ctype == CommandType.CREATE_WORKFLOW:
        return _handle_create_workflow(conn, env)
    if ctype == CommandType.ADVANCE_WORKFLOW:
        return _handle_advance_workflow(conn, env)
    if ctype == CommandType.RESOLVE_GOAL_APPROVAL:
        return _handle_resolve_goal_approval(conn, env)
    if ctype == CommandType.RECORD_VERIFICATION_RESULT:
        return _handle_record_verification_result(conn, env)
    return {"ok": False, "error": f"unhandled command_type: {ctype.value}"}


def _ensure_task(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Tuple[Optional[str], bool, bool]:
    """
    Returns (task_id_or_none, created_new_task_body, deduped).
    """
    if env.task_id:
        tid = env.task_id
        if not task_exists(conn, tid):
            return None, False, False
        return tid, False, False

    candidate = new_task_id()
    tid, fresh = claim_idempotency_and_create_task(
        conn,
        idem,
        candidate,
        env.channel.value,
    )
    if not fresh:
        return tid, False, True
    return tid, True, False


def _append(
    conn: sqlite3.Connection, task_id: str, et: EventType, payload: Dict[str, Any]
) -> int:
    return append_event(conn, task_id, et, payload)


def _user_message_scoped_idempotency_key(env: CommandEnvelope) -> Optional[str]:
    """Stable dedupe key for SubmitUserMessage on an existing task (e.g. Telegram retries)."""
    if not env.task_id:
        return None
    if env.idempotency_key and str(env.idempotency_key).strip():
        return normalize_scoped_idempotency_key(
            str(env.task_id),
            str(env.idempotency_key).strip(),
            env.command_type.value,
        )
    ext = (env.external_id or "").strip()
    if ext:
        return normalize_scoped_idempotency_key(
            env.channel.value,
            f"{ext}|{env.task_id}",
            env.command_type.value,
        )
    return None


def _resolve_principal_for_task(
    conn: sqlite3.Connection,
    env: CommandEnvelope,
    *,
    task_id: str,
) -> Tuple[str, bool]:
    existing = get_task_principal_id(conn, task_id)
    principal_id = resolve_principal_id(
        conn,
        channel=env.channel.value,
        payload=env.payload,
        principal_id_hint=existing or str(env.payload.get("principal_id") or ""),
    )
    link_task_principal(conn, task_id, principal_id, channel=env.channel.value)
    return principal_id, principal_id != (existing or "")


def _resolve_principal_for_command(
    conn: sqlite3.Connection,
    env: CommandEnvelope,
) -> str:
    hinted = str(env.payload.get("principal_id") or "").strip()
    if env.task_id:
        existing = get_task_principal_id(conn, env.task_id)
        if existing:
            return existing
    return resolve_principal_id(
        conn,
        channel=env.channel.value,
        payload=env.payload,
        principal_id_hint=hinted,
    )


def _capture_principal_preferences(
    conn: sqlite3.Connection, principal_id: str, payload: Dict[str, Any]
) -> None:
    pid = str(principal_id or "").strip()
    if not pid:
        return
    visibility_mode = str(payload.get("visibility_mode") or "").strip().lower()
    if visibility_mode in {"summary", "full"}:
        set_principal_preference(conn, pid, "visibility_mode", visibility_mode)
    collaboration_mode = str(payload.get("collaboration_mode") or "").strip().lower()
    if collaboration_mode and collaboration_mode != "auto":
        set_principal_preference(conn, pid, "collaboration_mode", collaboration_mode)
    preferred_model_family = str(payload.get("preferred_model_family") or "").strip()
    if preferred_model_family:
        set_principal_preference(
            conn, pid, "preferred_model_family", preferred_model_family
        )
    preferred_model_label = str(payload.get("preferred_model_label") or "").strip()
    if preferred_model_label:
        set_principal_preference(conn, pid, "preferred_model_label", preferred_model_label)
    locale = str(payload.get("locale") or "").strip()
    if locale:
        set_principal_preference(conn, pid, "locale", locale)


def _handle_create_task(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    tid, is_new, deduped = _ensure_task(conn, env, idem)
    if tid is None:
        return {"ok": False, "error": f"unknown task_id: {env.task_id}"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {"command_type": env.command_type.value, "idempotency_key": idem},
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": env.channel.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {
                "summary": env.payload.get("summary", ""),
                "channel": env.channel.value,
            },
        )
    else:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": env.channel.value},
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_user_message(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    if not env.task_id:
        # Without external_id / idempotency_key, do not collapse all channel messages into one task.
        idem_use = idem
        if not (env.external_id or "").strip() and not (env.idempotency_key or "").strip():
            idem_use = normalize_scoped_idempotency_key(
                "submit_user_message",
                uuid.uuid4().hex,
                env.command_type.value,
            )
        tid, is_new, deduped = _ensure_task(conn, env, idem_use)
        if tid is None:
            return {"ok": False, "error": "task resolution failed"}
        if deduped:
            _append(
                conn,
                tid,
                EventType.COMMAND_DEDUPED,
                {"command_type": env.command_type.value, "idempotency_key": idem_use},
            )
            return {"ok": True, "task_id": tid, "deduped": True}
        summary = str(
            env.payload.get("routing_text")
            or env.payload.get("text", "")
            or env.payload.get("summary", "")
        )[:120]
        if is_new:
            _append(
                conn,
                tid,
                EventType.COMMAND_RECEIVED,
                {"command_type": env.command_type.value, "channel": env.channel.value},
            )
            _append(
                conn,
                tid,
                EventType.TASK_CREATED,
                {"summary": summary, "channel": env.channel.value},
            )
        principal_id, principal_changed = _resolve_principal_for_task(conn, env, task_id=tid)
        if principal_changed:
            _append(
                conn,
                tid,
                EventType.PRINCIPAL_LINKED,
                {
                    "principal_id": principal_id,
                    "channel": env.channel.value,
                },
            )
        _capture_principal_preferences(conn, principal_id, env.payload)
        um: Dict[str, Any] = {
            "text": env.payload.get("text", ""),
            "routing_text": env.payload.get("routing_text", ""),
            "mention_targets": env.payload.get("mention_targets", []),
            "model_mentions": env.payload.get("model_mentions", []),
            "preferred_model_family": env.payload.get("preferred_model_family", ""),
            "preferred_model_label": env.payload.get("preferred_model_label", ""),
            "routing_hint": env.payload.get("routing_hint", ""),
            "collaboration_mode": env.payload.get("collaboration_mode", ""),
            "visibility_mode": env.payload.get("visibility_mode", ""),
            "channel": env.channel.value,
            "chat_id": env.payload.get("chat_id"),
            "chat_type": env.payload.get("chat_type"),
            "message_id": env.payload.get("message_id"),
            "from_user": env.payload.get("from_user"),
            "from_username": env.payload.get("from_username"),
            "principal_id": principal_id,
        }
        if env.payload.get("message_thread_id") is not None:
            um["message_thread_id"] = env.payload.get("message_thread_id")
        if env.payload.get("telegram_continuation"):
            um["telegram_continuation"] = True
        if env.payload.get("telegram_continuation_anchor_message_id") is not None:
            um["telegram_continuation_anchor_message_id"] = env.payload.get(
                "telegram_continuation_anchor_message_id"
            )
        _append(conn, tid, EventType.USER_MESSAGE, um)
        if env.external_id:
            _append(
                conn,
                tid,
                EventType.EXTERNAL_REF,
                {"kind": f"{env.channel.value}_update", "ref": env.external_id},
            )
        queued_cursor_job = (
            env.channel == Channel.TELEGRAM
            and bool(env.payload.get("auto_cursor_job", False))
        )
        if queued_cursor_job:
            _append(
                conn,
                tid,
                EventType.JOB_QUEUED,
                {
                    "kind": "cursor",
                    "prompt_excerpt": str(
                        env.payload.get("routing_text") or env.payload.get("text", "")
                    )[:300],
                    "source": "telegram_default",
                },
            )
        return {
            "ok": True,
            "task_id": tid,
            "deduped": False,
            "queued_cursor_job": queued_cursor_job,
        }
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    scoped = _user_message_scoped_idempotency_key(env)
    if scoped:
        outcome = claim_scoped_idempotency(conn, scoped, tid)
        if outcome == "conflict":
            return {"ok": False, "error": "idempotency_key_conflict"}
        if outcome == "duplicate":
            _append(
                conn,
                tid,
                EventType.COMMAND_DEDUPED,
                {
                    "command_type": env.command_type.value,
                    "idempotency_key": scoped,
                    "scoped": True,
                },
            )
            return {"ok": True, "task_id": tid, "deduped": True}
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value},
    )
    principal_id, principal_changed = _resolve_principal_for_task(conn, env, task_id=tid)
    if principal_changed:
        _append(
            conn,
            tid,
            EventType.PRINCIPAL_LINKED,
            {
                "principal_id": principal_id,
                "channel": env.channel.value,
            },
        )
    _capture_principal_preferences(conn, principal_id, env.payload)
    um2: Dict[str, Any] = {
        "text": env.payload.get("text", ""),
        "routing_text": env.payload.get("routing_text", ""),
        "mention_targets": env.payload.get("mention_targets", []),
        "model_mentions": env.payload.get("model_mentions", []),
        "preferred_model_family": env.payload.get("preferred_model_family", ""),
        "preferred_model_label": env.payload.get("preferred_model_label", ""),
        "routing_hint": env.payload.get("routing_hint", ""),
        "collaboration_mode": env.payload.get("collaboration_mode", ""),
        "visibility_mode": env.payload.get("visibility_mode", ""),
        "channel": env.channel.value,
        "chat_id": env.payload.get("chat_id"),
        "chat_type": env.payload.get("chat_type"),
        "message_id": env.payload.get("message_id"),
        "from_user": env.payload.get("from_user"),
        "from_username": env.payload.get("from_username"),
        "principal_id": principal_id,
    }
    if env.payload.get("message_thread_id") is not None:
        um2["message_thread_id"] = env.payload.get("message_thread_id")
    if env.payload.get("telegram_continuation"):
        um2["telegram_continuation"] = True
    if env.payload.get("telegram_continuation_anchor_message_id") is not None:
        um2["telegram_continuation_anchor_message_id"] = env.payload.get(
            "telegram_continuation_anchor_message_id"
        )
    _append(conn, tid, EventType.USER_MESSAGE, um2)
    if env.external_id:
        _append(
            conn,
            tid,
            EventType.EXTERNAL_REF,
            {
                "kind": f"{env.channel.value}_update",
                "ref": env.external_id,
            },
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _coerce_feedback_score(payload: Dict[str, Any]) -> float:
    raw = payload.get("score")
    if raw is None:
        label = str(payload.get("label") or "").strip().lower()
        if label in {"positive", "upvote", "thumbs_up", "good"}:
            return 1.0
        if label in {"negative", "downvote", "thumbs_down", "bad"}:
            return -1.0
        return 0.0
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0:
        return 1.0
    if score < -1.0:
        return -1.0
    return score


def _handle_user_feedback(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    if not env.task_id:
        return {"ok": False, "error": "task_id required"}
    tid = str(env.task_id)
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    label = str(env.payload.get("label") or "").strip()
    payload = {
        "label": label,
        "score": _coerce_feedback_score(env.payload),
        "comment": str(env.payload.get("comment") or "").strip(),
        "source": str(env.payload.get("source") or env.channel.value),
        "feedback_id": str(env.payload.get("feedback_id") or env.external_id or ""),
    }
    _append(conn, tid, EventType.USER_FEEDBACK, payload)
    return {"ok": True, "task_id": tid}


def _handle_create_cursor_job(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    tid, is_new, deduped = _ensure_task(conn, env, idem)
    if tid is None:
        return {"ok": False, "error": f"unknown task_id: {env.task_id}"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {
                "command_type": env.command_type.value,
                "idempotency_key": idem,
            },
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {"summary": env.payload.get("summary", "cursor job"), "channel": env.channel.value},
        )
    agent_hint = env.payload.get("cursor_agent_id")
    _append(
        conn,
        tid,
        EventType.JOB_QUEUED,
        {
            "kind": "cursor",
            "prompt_excerpt": str(env.payload.get("prompt", ""))[:300],
            "cursor_agent_id": agent_hint,
        },
    )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_cursor_followup(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    if not env.task_id:
        return {"ok": False, "error": "task_id required"}
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    from .execution_runtime import continue_cursor_followup_for_task

    text = str(
        env.payload.get("prompt") or env.payload.get("text") or env.payload.get("message") or ""
    ).strip()
    if not text:
        return {"ok": False, "error": "prompt or text required"}
    out = continue_cursor_followup_for_task(conn, tid, text)
    merged = {"task_id": tid, **out}
    if not out.get("ok"):
        return {"ok": False, **merged}
    return {"ok": True, **merged}


def _handle_cursor_stop(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    if not env.task_id:
        return {"ok": False, "error": "task_id required"}
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    _append(conn, tid, EventType.JOB_FAILED, {"error": "stopped_by_user", "detail": env.payload})
    return {"ok": True, "task_id": tid}


def _finalize_reported_cursor_completion(
    conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    from .plan_runtime import (
        finalize_execute_step_verification,
        record_verification_event_payload,
    )
    from .projector import project_task_dict
    from .user_surface import sanitize_user_surface_text

    channel = get_task_channel(conn, task_id)
    if not channel:
        return None
    proj = project_task_dict(conn, task_id, channel)
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    plan_meta = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}
    execution_meta = (
        meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    )
    plan_id = str(plan_meta.get("plan_id") or "").strip()
    step_id = str(plan_meta.get("execute_step_id") or "").strip()
    if not plan_id or not step_id:
        return None
    if (
        str(plan_meta.get("last_verified_step_id") or "").strip() == step_id
        and str(plan_meta.get("last_verification_id") or "").strip()
    ):
        return None

    lane = str(payload.get("execution_lane") or execution_meta.get("lane") or "").strip()
    agent_url = str(payload.get("agent_url") or "").strip()
    pr_url = str(payload.get("pr_url") or "").strip()
    terminal_status = str(payload.get("status") or "FINISHED").strip() or "FINISHED"
    fv = finalize_execute_step_verification(
        conn,
        task_id=task_id,
        plan_id=plan_id,
        execute_step_id=step_id,
        terminal_status=terminal_status,
        pr_url=pr_url,
        agent_url=agent_url,
        lane=lane,
    )
    verification_id = str(fv.get("verification_id") or "").strip()
    if not verification_id:
        return None

    _append(
        conn,
        task_id,
        EventType.VERIFICATION_RECORDED,
        record_verification_event_payload(
            plan_id=plan_id,
            step_id=step_id,
            verification_id=verification_id,
            verdict=str(fv.get("verdict") or ""),
            summary=str(fv.get("summary") or ""),
            method="cursor_report",
        ),
    )
    collab_payload = fv.get("collaboration_event_payload")
    if isinstance(collab_payload, dict) and collab_payload:
        _append(conn, task_id, EventType.COLLABORATION_RECORDED, collab_payload)
    act_payload = fv.get("activation_event_payload")
    if isinstance(act_payload, dict) and act_payload:
        _append(conn, task_id, EventType.ACTIVATION_DECISION_RECORDED, act_payload)
    out_payload = fv.get("collaboration_outcome_event_payload")
    if isinstance(out_payload, dict) and out_payload:
        _append(conn, task_id, EventType.COLLABORATION_OUTCOME_RECORDED, out_payload)
    rep_payload = fv.get("repair_outcome_event_payload")
    if isinstance(rep_payload, dict) and rep_payload:
        _append(conn, task_id, EventType.REPAIR_OUTCOME_RECORDED, rep_payload)

    if not fv.get("should_complete_job", True):
        scen_rx = str(fv.get("scenario_user_receipt") or "").strip()
        collab_note = str(fv.get("collaboration_user_note") or "").strip()
        fallback = "The execution reported finished, but verification did not accept the outcome."
        user_safe_error = (
            sanitize_user_surface_text(scen_rx, fallback=fallback, limit=1200) or fallback
        )
        if collab_note:
            user_safe_error = (
                sanitize_user_surface_text(
                    f"{user_safe_error}\n\n{collab_note}",
                    fallback=user_safe_error,
                    limit=1400,
                )
                or user_safe_error
            )
        fail_payload: Dict[str, Any] = {
            "error": "verification_failed",
            "message": str(fv.get("summary") or "")[:1500],
            "user_safe_error": user_safe_error,
            "cursor_agent_id": payload.get("cursor_agent_id"),
            "agent_url": agent_url or None,
            "pr_url": pr_url or None,
            "backend": str(payload.get("backend") or "cursor"),
            "runner": str(payload.get("runner") or "cursor"),
            "execution_lane": lane or str(execution_meta.get("lane") or "cursor"),
        }
        if payload.get("attempt_id"):
            fail_payload["attempt_id"] = payload.get("attempt_id")
        _append(conn, task_id, EventType.JOB_FAILED, fail_payload)
        return {
            "ok": True,
            "task_id": task_id,
            "verification_id": verification_id,
            "verification_failed": True,
        }

    completed_payload = dict(payload)
    if "backend" not in completed_payload:
        completed_payload["backend"] = "cursor"
    if "runner" not in completed_payload:
        completed_payload["runner"] = "cursor"
    if lane and "execution_lane" not in completed_payload:
        completed_payload["execution_lane"] = lane
    _append(conn, task_id, EventType.JOB_COMPLETED, completed_payload)
    return {
        "ok": True,
        "task_id": task_id,
        "verification_id": verification_id,
        "verification_failed": False,
    }


def _handle_cursor_report(conn: sqlite3.Connection, env: CommandEnvelope, idem: str) -> Dict[str, Any]:
    """Internal: map cursor_openclaw lifecycle JSON into events."""
    tid = env.task_id or env.payload.get("task_id")
    if not tid:
        return {"ok": False, "error": "task_id required in payload or envelope"}
    tid = str(tid)
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    et_raw = env.payload.get("event_type") or env.payload.get("cursor_event")
    if not et_raw:
        return {"ok": False, "error": "payload.event_type required"}
    try:
        cet = EventType(str(et_raw))
    except ValueError:
        return {"ok": False, "error": f"invalid event_type: {et_raw}"}
    if env.payload.get("payload") is not None and not isinstance(env.payload.get("payload"), dict):
        return {"ok": False, "error": "payload.payload must be a JSON object"}
    inner = env.payload.get("payload") if isinstance(env.payload.get("payload"), dict) else {}
    inner_canon = json.dumps(inner, sort_keys=True, ensure_ascii=False, default=str)
    inner_hash = hashlib.sha256(inner_canon.encode("utf-8")).hexdigest()[:32]
    report_key = normalize_scoped_idempotency_key(
        str(tid), f"{cet.value}|{inner_hash}", "ReportCursorEvent"
    )
    outcome = claim_scoped_idempotency(conn, report_key, tid)
    if outcome == "conflict":
        return {"ok": False, "error": "cursor_report_idempotency_conflict"}
    if outcome == "duplicate":
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {
                "command_type": env.command_type.value,
                "idempotency_key": report_key,
                "scoped": True,
            },
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    _append(conn, tid, EventType.COMMAND_RECEIVED, {"command_type": "ReportCursorEvent"})
    if cet == EventType.JOB_COMPLETED:
        finalized = _finalize_reported_cursor_completion(conn, tid, inner)
        if finalized is not None:
            return finalized
    _append(conn, tid, cet, inner)
    return {"ok": True, "task_id": tid}


def _handle_alexa(conn: sqlite3.Connection, env: CommandEnvelope, idem: str) -> Dict[str, Any]:
    text = str(env.payload.get("utterance") or env.payload.get("text") or "").strip()
    routing_text = str(env.payload.get("routing_text") or text).strip()
    fake = CommandEnvelope(
        command_type=CommandType.CREATE_TASK,
        channel=Channel.ALEXA,
        payload={"summary": routing_text[:200] or text[:200] or "alexa"},
        external_id=env.external_id,
        idempotency_key=env.idempotency_key,
    )
    idem_use = fake.resolved_idempotency_key()
    tid, is_new, deduped = _ensure_task(conn, fake, idem_use)
    if tid is None:
        return {"ok": False, "error": "task resolution failed"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {"command_type": env.command_type.value},
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": Channel.ALEXA.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {"summary": routing_text[:200] or text[:200] or "alexa", "channel": Channel.ALEXA.value},
        )
    principal_id, principal_changed = _resolve_principal_for_task(conn, env, task_id=tid)
    if principal_changed:
        _append(
            conn,
            tid,
            EventType.PRINCIPAL_LINKED,
            {
                "principal_id": principal_id,
                "channel": Channel.ALEXA.value,
            },
        )
    _capture_principal_preferences(conn, principal_id, env.payload)
    if text:
        _append(
            conn,
            tid,
            EventType.USER_MESSAGE,
            {
                "text": text,
                "routing_text": routing_text,
                "channel": Channel.ALEXA.value,
                "session_id": env.payload.get("session_id"),
                "request_id": env.payload.get("request_id"),
                "intent_name": env.payload.get("intent_name"),
                "locale": env.payload.get("locale"),
                "user_id": env.payload.get("user_id"),
                "device_id": env.payload.get("device_id"),
                "principal_id": principal_id,
            },
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_publish_capability_snapshot(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    ensure_system_task(conn)
    blob = dict(env.payload)
    blob["published_ts"] = time.time()
    set_meta(conn, META_DIGEST_KEY, json.dumps(blob, ensure_ascii=False))
    set_meta(conn, META_DIGEST_TS_KEY, str(blob["published_ts"]))
    rows = blob.get("rows") if isinstance(blob.get("rows"), list) else []
    excerpt = json.dumps({"summary": blob.get("summary"), "row_count": len(rows)})[
        :480
    ]
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.CAPABILITY_SNAPSHOT,
        {"summary_json_excerpt": excerpt, "channel": env.channel.value},
    )
    return {"ok": True, "published_ts": blob["published_ts"]}


def _handle_heal_runtime_capability(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    from .optimizer import heal_runtime_capability

    ensure_system_task(conn)
    skill_key = str(env.payload.get("skill_key") or env.payload.get("skill") or "").strip()
    if not skill_key:
        return {"ok": False, "error": "skill_key required"}
    return heal_runtime_capability(
        conn,
        skill_key=skill_key,
        install_slug=str(env.payload.get("install_slug") or "").strip(),
        actor=str(env.payload.get("actor") or env.channel.value),
        allow_install=bool(env.payload.get("allow_install", True)),
        allow_update_all=bool(env.payload.get("allow_update_all", True)),
        allow_config_repair=bool(env.payload.get("allow_config_repair", True)),
    )


def _resolve_audit_task_id(conn: sqlite3.Connection, env: CommandEnvelope) -> str:
    if env.task_id:
        tid = str(env.task_id)
        if not task_exists(conn, tid):
            raise ValueError(f"unknown task_id: {tid}")
        return tid
    ensure_system_task(conn)
    return SYSTEM_TASK_ID


def _handle_record_evaluation_finding(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    try:
        tid = _resolve_audit_task_id(conn, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    payload = {
        "run_id": str(env.payload.get("run_id") or ""),
        "category": str(env.payload.get("category") or ""),
        "severity": str(env.payload.get("severity") or "medium"),
        "summary": str(env.payload.get("summary") or ""),
        "count": int(env.payload.get("count") or 1),
        "evidence_task_ids": env.payload.get("evidence_task_ids", []),
        "recommended_action": str(env.payload.get("recommended_action") or ""),
    }
    _append(conn, tid, EventType.EVALUATION_RECORDED, payload)
    return {"ok": True, "task_id": tid}


def _handle_create_optimization_proposal(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    try:
        tid = _resolve_audit_task_id(conn, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    payload = {
        "proposal_id": str(env.payload.get("proposal_id") or uuid.uuid4().hex[:12]),
        "title": str(env.payload.get("title") or "Untitled optimization proposal"),
        "category": str(env.payload.get("category") or ""),
        "status": str(env.payload.get("status") or "proposed"),
        "problem_statement": str(env.payload.get("problem_statement") or ""),
        "recommended_action": str(env.payload.get("recommended_action") or ""),
        "target_files": env.payload.get("target_files", []),
        "preferred_execution_lane": str(
            env.payload.get("preferred_execution_lane") or "cursor_branch_prep"
        ),
        "analysis_lane": str(env.payload.get("analysis_lane") or "openclaw"),
        "branch_prep_allowed": bool(env.payload.get("branch_prep_allowed", False)),
        "gate_reasons": env.payload.get("gate_reasons", []),
        "evidence_task_ids": env.payload.get("evidence_task_ids", []),
    }
    _append(conn, tid, EventType.OPTIMIZATION_PROPOSAL, payload)
    return {"ok": True, "task_id": tid, "proposal_id": payload["proposal_id"]}


def _handle_run_optimization_cycle(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    from .optimizer import run_optimization_cycle

    ensure_system_task(conn)
    regression_report = (
        dict(env.payload.get("regression_report"))
        if isinstance(env.payload.get("regression_report"), dict)
        else {}
    )
    required_skills = env.payload.get("required_skills")
    if not isinstance(required_skills, list):
        required_skills = []
    return run_optimization_cycle(
        conn,
        limit=max(1, int(env.payload.get("limit") or 60)),
        regression_report=regression_report,
        required_skills=[str(v) for v in required_skills if str(v).strip()],
        emit_proposals=bool(env.payload.get("emit_proposals", True)),
        actor=str(env.payload.get("actor") or env.channel.value),
        analysis_mode=str(env.payload.get("analysis_mode") or "heuristic"),
        repo_path=Path(
            str(
                env.payload.get("repo_path")
                or os.environ.get("ANDREA_SYNC_CURSOR_REPO")
                or REPO_ROOT
            )
        ),
        auto_apply_ready=bool(env.payload.get("auto_apply_ready", False)),
        idle_seconds=float(env.payload.get("idle_seconds") or 120.0),
    )


def _handle_apply_optimization_proposal(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    from .optimizer import apply_optimization_proposal

    ensure_system_task(conn)
    payload = dict(env.payload)
    repo_path = Path(
        str(
            payload.get("repo_path")
            or os.environ.get("ANDREA_SYNC_CURSOR_REPO")
            or REPO_ROOT
        )
    )
    return apply_optimization_proposal(
        conn,
        proposal_payload=payload,
        repo_path=repo_path,
        actor=str(env.payload.get("actor") or env.channel.value),
    )


def _handle_run_incident_repair(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    from .repair_orchestrator import run_incident_repair_cycle

    ensure_system_task(conn)
    incident_payload = (
        dict(env.payload.get("incident"))
        if isinstance(env.payload.get("incident"), dict)
        else {}
    )
    verification_report = (
        dict(env.payload.get("verification_report"))
        if isinstance(env.payload.get("verification_report"), dict)
        else {}
    )
    runtime_error = (
        dict(env.payload.get("runtime_error"))
        if isinstance(env.payload.get("runtime_error"), dict)
        else {}
    )
    health_failure = (
        dict(env.payload.get("health_failure"))
        if isinstance(env.payload.get("health_failure"), dict)
        else {}
    )
    log_alert = (
        dict(env.payload.get("log_alert"))
        if isinstance(env.payload.get("log_alert"), dict)
        else {}
    )
    return run_incident_repair_cycle(
        conn,
        repo_path=Path(
            str(
                env.payload.get("repo_path")
                or os.environ.get("ANDREA_SYNC_CURSOR_REPO")
                or REPO_ROOT
            )
        ),
        actor=str(env.payload.get("actor") or env.channel.value),
        incident_payload=incident_payload,
        verification_report=verification_report,
        runtime_error=runtime_error,
        health_failure=health_failure,
        log_alert=log_alert,
        source_task_id=str(env.payload.get("source_task_id") or ""),
        incident_id=str(env.payload.get("incident_id") or ""),
        cursor_execute=bool(env.payload.get("cursor_execute", False)),
        write_report=bool(env.payload.get("write_report", True)),
    )


def _handle_save_principal_memory(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    try:
        tid = _resolve_audit_task_id(conn, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    principal_id = _resolve_principal_for_command(conn, env)
    if env.task_id and task_exists(conn, str(env.task_id)):
        link_task_principal(conn, str(env.task_id), principal_id, channel=env.channel.value)
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    content = str(env.payload.get("content") or env.payload.get("text") or "").strip()
    if not content:
        return {"ok": False, "error": "content required"}
    memory_id = save_principal_memory(
        conn,
        principal_id,
        content=content,
        kind=str(env.payload.get("kind") or "note"),
        source=str(env.payload.get("source") or env.channel.value),
        source_task_id=str(env.task_id or tid),
        memory_id=str(env.payload.get("memory_id") or ""),
    )
    _append(
        conn,
        tid,
        EventType.PRINCIPAL_MEMORY_SAVED,
        {
            "principal_id": principal_id,
            "memory_id": memory_id,
            "kind": str(env.payload.get("kind") or "note"),
            "content": content,
        },
    )
    return {"ok": True, "task_id": tid, "principal_id": principal_id, "memory_id": memory_id}


def _handle_set_principal_preference(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    try:
        tid = _resolve_audit_task_id(conn, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    principal_id = _resolve_principal_for_command(conn, env)
    if env.task_id and task_exists(conn, str(env.task_id)):
        link_task_principal(conn, str(env.task_id), principal_id, channel=env.channel.value)
    key = str(env.payload.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "key required"}
    value = env.payload.get("value")
    set_principal_preference(conn, principal_id, key, value)
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    _append(
        conn,
        tid,
        EventType.PRINCIPAL_PREFERENCE_UPDATED,
        {
            "principal_id": principal_id,
            "key": key,
            "value": value,
        },
    )
    return {"ok": True, "task_id": tid, "principal_id": principal_id, "key": key}


def _handle_link_principal_identity(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    ensure_system_task(conn)
    principal_id = str(env.payload.get("principal_id") or "").strip()
    channel = str(env.payload.get("channel") or "").strip().lower()
    external_key = str(env.payload.get("external_key") or "").strip()
    if not principal_id or not channel or not external_key:
        return {"ok": False, "error": "principal_id, channel, and external_key required"}
    link_principal_identity(
        conn,
        principal_id,
        channel=channel,
        external_key=external_key,
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.PRINCIPAL_LINKED,
        {
            "principal_id": principal_id,
            "channel": channel,
            "external_key": external_key,
        },
    )
    return {"ok": True, "task_id": SYSTEM_TASK_ID, "principal_id": principal_id}


def _resolve_reminder_target(principal_id: str) -> str:
    return (
        os.environ.get("ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()


def _handle_create_reminder(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    try:
        tid = _resolve_audit_task_id(conn, env)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    principal_id = _resolve_principal_for_command(conn, env)
    if env.task_id and task_exists(conn, str(env.task_id)):
        link_task_principal(conn, str(env.task_id), principal_id, channel=env.channel.value)
    message = str(env.payload.get("message") or env.payload.get("text") or "").strip()
    if not message:
        return {"ok": False, "error": "message required"}
    try:
        due_at = float(env.payload.get("due_at"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "due_at must be a unix timestamp"}
    delivery_channel = str(env.payload.get("delivery_channel") or "telegram").strip().lower()
    delivery_target = str(env.payload.get("delivery_target") or "").strip()
    if not delivery_target and delivery_channel == Channel.TELEGRAM.value:
        delivery_target = get_principal_recent_telegram_chat_id(conn, principal_id) or _resolve_reminder_target(
            principal_id
        )
    status = "scheduled" if delivery_target else "awaiting_delivery_channel"
    reminder_id = create_reminder(
        conn,
        principal_id=principal_id,
        channel=delivery_channel,
        delivery_target=delivery_target,
        message=message,
        due_at=due_at,
        status=status,
        source_task_id=str(env.task_id or tid),
        metadata={
            "source_channel": env.channel.value,
            "note": str(env.payload.get("note") or ""),
        },
        reminder_id=str(env.payload.get("reminder_id") or ""),
    )
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    _append(
        conn,
        tid,
        EventType.REMINDER_CREATED,
        {
            "principal_id": principal_id,
            "reminder_id": reminder_id,
            "message": message,
            "due_at": due_at,
            "status": status,
            "delivery_channel": delivery_channel,
            "delivery_target": delivery_target,
        },
    )
    try:
        from .assistant_receipts import try_record_reminder_receipt
        from .domain_repairs import suggest_missing_reminder_target_repair

        try_record_reminder_receipt(
            conn,
            task_id=tid,
            reminder_id=reminder_id,
            message=message,
            due_at=due_at,
            status=status,
            delivery_channel=delivery_channel,
            delivery_target=delivery_target,
            principal_id=principal_id,
        )
        if str(status or "") == "awaiting_delivery_channel":
            suggest_missing_reminder_target_repair(
                conn,
                task_id=tid,
                reminder_id=reminder_id,
                principal_id=principal_id,
            )
    except Exception:
        pass
    return {
        "ok": True,
        "task_id": tid,
        "principal_id": principal_id,
        "reminder_id": reminder_id,
        "status": status,
    }


def _handle_run_proactive_sweep(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    from .adapters import telegram as tg_adapt

    ensure_system_task(conn)
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value, "channel": env.channel.value},
    )
    due = list_due_reminders(
        conn,
        now_ts=float(env.payload.get("now_ts") or time.time()),
        limit=max(1, int(env.payload.get("limit") or 20)),
    )
    delivered = 0
    failed = 0
    awaiting = 0
    for row in due:
        reminder_id = str(row.get("reminder_id") or "")
        principal_id = str(row.get("principal_id") or "")
        source_task_id = str(row.get("source_task_id") or "")
        task_id = source_task_id if source_task_id and task_exists(conn, source_task_id) else SYSTEM_TASK_ID
        target = str(row.get("delivery_target") or "").strip()
        if not target and str(row.get("channel") or "") == Channel.TELEGRAM.value:
            target = get_principal_recent_telegram_chat_id(conn, principal_id) or _resolve_reminder_target(
                principal_id
            )
            if target:
                update_reminder(conn, reminder_id, delivery_target=target)
        if not target:
            update_reminder(conn, reminder_id, status="awaiting_delivery_channel")
            awaiting += 1
            continue
        _append(
            conn,
            task_id,
            EventType.REMINDER_TRIGGERED,
            {
                "principal_id": principal_id,
                "reminder_id": reminder_id,
                "message": str(row.get("message") or ""),
                "delivery_target": target,
            },
        )
        try:
            tg_adapt.send_text_message(
                bot_token=str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
                chat_id=target,
                text="\n".join(
                    [
                        "Andrea:",
                        f"Reminder: {str(row.get('message') or '').strip()}",
                    ]
                ),
            )
            update_reminder(
                conn,
                reminder_id,
                status="delivered",
                metadata={"delivered_at": time.time()},
            )
            _append(
                conn,
                task_id,
                EventType.REMINDER_DELIVERED,
                {
                    "principal_id": principal_id,
                    "reminder_id": reminder_id,
                    "delivery_target": target,
                },
            )
            try:
                from .assistant_followthrough import on_reminder_lifecycle_event

                on_reminder_lifecycle_event(
                    conn,
                    task_id=task_id,
                    event_name="delivered",
                    payload={
                        "principal_id": principal_id,
                        "reminder_id": reminder_id,
                        "delivery_target": target,
                    },
                )
            except Exception:
                pass
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            update_reminder(
                conn,
                reminder_id,
                status="failed",
                metadata={"error": str(exc)},
            )
            _append(
                conn,
                task_id,
                EventType.REMINDER_FAILED,
                {
                    "principal_id": principal_id,
                    "reminder_id": reminder_id,
                    "delivery_target": target,
                    "error": str(exc),
                },
            )
            try:
                from .assistant_followthrough import on_reminder_lifecycle_event

                on_reminder_lifecycle_event(
                    conn,
                    task_id=task_id,
                    event_name="failed",
                    payload={
                        "principal_id": principal_id,
                        "reminder_id": reminder_id,
                        "delivery_target": target,
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
            failed += 1
    ft_poll: list = []
    try:
        from .assistant_followthrough import poll_due_workflows_for_followthrough

        ft_poll = poll_due_workflows_for_followthrough(conn, limit=12)
    except Exception:
        ft_poll = []
    return {
        "ok": True,
        "task_id": SYSTEM_TASK_ID,
        "delivered": delivered,
        "failed": failed,
        "awaiting_delivery_channel": awaiting,
        "due_count": len(due),
        "followthrough_workflow_signals": ft_poll,
    }


def _handle_kill_switch_engage(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    engage_kill_switch(
        conn,
        reason=str(env.payload.get("reason") or ""),
        source=str(env.payload.get("source") or env.channel.value),
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.KILL_SWITCH_ENGAGED,
        {"reason": env.payload.get("reason", ""), "source": env.channel.value},
    )
    return {"ok": True, "kill_switch": True}


def _handle_kill_switch_release(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    release_kill_switch(conn)
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.KILL_SWITCH_RELEASED,
        {"source": env.channel.value},
    )
    return {"ok": True, "kill_switch": False}


def _handle_create_goal(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    principal_id = str(env.payload.get("principal_id") or "").strip()
    if not principal_id:
        principal_id = _resolve_principal_for_command(conn, env)
    if not principal_id:
        return {"ok": False, "error": "principal_id required"}
    summary = str(env.payload.get("summary") or "")[:2000]
    channel = str(env.payload.get("channel") or env.channel.value or "internal")
    meta = env.payload.get("metadata")
    metadata = meta if isinstance(meta, dict) else {}
    gid = create_goal(conn, principal_id, summary, channel=channel, metadata=metadata)
    append_goal_event(
        conn,
        gid,
        "created",
        {"principal_id": principal_id, "summary": summary[:500]},
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.GOAL_CREATED,
        {"goal_id": gid, "principal_id": principal_id},
    )
    return {"ok": True, "goal_id": gid, "principal_id": principal_id}


def _handle_link_task_to_goal(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    task_id = str(env.task_id or env.payload.get("task_id") or "").strip()
    goal_id = str(env.payload.get("goal_id") or "").strip()
    if not task_id or not goal_id:
        return {"ok": False, "error": "task_id and goal_id required"}
    if not task_exists(conn, task_id):
        return {"ok": False, "error": "unknown task"}
    goal_row = get_goal(conn, goal_id)
    if not goal_row:
        return {"ok": False, "error": "unknown goal"}
    pid_task = get_task_principal_id(conn, task_id)
    if pid_task and str(goal_row["principal_id"]) != pid_task:
        return {"ok": False, "error": "principal_mismatch"}
    link_task_to_goal(conn, task_id, goal_id)
    _append(
        conn,
        task_id,
        EventType.TASK_GOAL_LINKED,
        {
            "goal_id": goal_id,
            "goal_summary": str(goal_row.get("summary") or ""),
            "goal_status": str(goal_row.get("status") or ""),
        },
    )
    append_goal_event(conn, goal_id, "task_linked", {"task_id": task_id})
    return {"ok": True, "task_id": task_id, "goal_id": goal_id}


def _handle_update_goal_status(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    goal_id = str(env.payload.get("goal_id") or "").strip()
    status = str(env.payload.get("status") or "").strip()
    if not goal_id or not status:
        return {"ok": False, "error": "goal_id and status required"}
    if not update_goal_status(conn, goal_id, status):
        return {"ok": False, "error": "goal not found"}
    append_goal_event(conn, goal_id, "status_changed", {"status": status})
    g = get_goal(conn, goal_id) or {}
    gsummary = str(g.get("summary") or "")
    for linked_tid in list_tasks_for_goal(conn, goal_id, limit=200):
        _append(
            conn,
            linked_tid,
            EventType.GOAL_STATUS_CHANGED,
            {"goal_id": goal_id, "status": status, "summary": gsummary},
        )
    return {"ok": True, "goal_id": goal_id, "status": status}


def _handle_record_goal_artifact(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    goal_id = str(env.payload.get("goal_id") or "").strip()
    task_id = str(env.payload.get("task_id") or env.task_id or "").strip()
    if not goal_id:
        return {"ok": False, "error": "goal_id required"}
    meta = env.payload.get("metadata")
    metadata = meta if isinstance(meta, dict) else {}
    aid = record_goal_artifact(
        conn,
        goal_id,
        task_id=task_id,
        kind=str(env.payload.get("kind") or "file"),
        label=str(env.payload.get("label") or ""),
        uri=str(env.payload.get("uri") or ""),
        metadata=metadata,
    )
    if task_id and task_exists(conn, task_id):
        _append(
            conn,
            task_id,
            EventType.GOAL_ARTIFACT_RECORDED,
            {
                "artifact_id": aid,
                "goal_id": goal_id,
                "label": env.payload.get("label"),
                "uri": env.payload.get("uri"),
                "kind": env.payload.get("kind"),
            },
        )
    append_goal_event(
        conn, goal_id, "artifact_recorded", {"artifact_id": aid, "task_id": task_id}
    )
    return {"ok": True, "artifact_id": aid}


def _handle_create_workflow(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    principal_id = str(env.payload.get("principal_id") or "").strip()
    if not principal_id:
        principal_id = _resolve_principal_for_command(conn, env)
    if not principal_id:
        return {"ok": False, "error": "principal_id required"}
    name = str(env.payload.get("name") or "workflow")[:500]
    defn = env.payload.get("definition")
    definition = defn if isinstance(defn, dict) else {}
    status = str(env.payload.get("status") or "draft")
    next_run = float(env.payload.get("next_run_at") or 0.0)
    wid = create_workflow(
        conn,
        principal_id,
        name,
        definition=definition,
        status=status,
        next_run_at=next_run,
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.WORKFLOW_CREATED,
        {"workflow_id": wid, "principal_id": principal_id, "name": name},
    )
    return {"ok": True, "workflow_id": wid, "principal_id": principal_id}


def _handle_advance_workflow(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    workflow_id = str(env.payload.get("workflow_id") or "").strip()
    step = str(env.payload.get("step") or "")
    if not workflow_id:
        return {"ok": False, "error": "workflow_id required"}
    wf = get_workflow(conn, workflow_id)
    if not wf:
        return {"ok": False, "error": "unknown workflow"}
    raw_def = wf.get("definition_json") or "{}"
    try:
        definition = json.loads(raw_def) if isinstance(raw_def, str) else dict(raw_def)
    except json.JSONDecodeError:
        definition = {}
    if not isinstance(definition, dict):
        definition = {}
    steps_done = definition.get("completed_steps")
    if not isinstance(steps_done, list):
        steps_done = []
    if step and step not in steps_done:
        steps_done.append(step)
    definition["completed_steps"] = steps_done
    update_workflow(conn, workflow_id, definition=definition, status="running")
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.WORKFLOW_STEP_ADVANCED,
        {"workflow_id": workflow_id, "step": step},
    )
    return {"ok": True, "workflow_id": workflow_id, "step": step}


def _handle_resolve_goal_approval(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    from .plan_runtime import resolve_goal_approval_command

    task_id = str(env.task_id or env.payload.get("task_id") or "").strip()
    approval_id = str(env.payload.get("approval_id") or "").strip()
    resolution = str(env.payload.get("resolution") or "").strip()
    if not task_id or not task_exists(conn, task_id):
        return {"ok": False, "error": "task_id required"}
    if not approval_id:
        return {"ok": False, "error": "approval_id required"}
    result = resolve_goal_approval_command(conn, task_id, approval_id, resolution)
    if not result.get("ok"):
        return result
    if result.get("resolution") == "rejected":
        _append(
            conn,
            task_id,
            EventType.ORCHESTRATION_STEP,
            {
                "phase": "plan",
                "status": "blocked",
                "lane": "policy",
                "summary": "Delegated execution was not approved; plan paused.",
                "plan_status": "blocked",
            },
        )
        _append(
            conn,
            task_id,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "I stopped the delegated plan because approval was not granted.",
                "route": "direct",
                "reason": "approval_denied",
            },
        )
        return {"ok": True, "task_id": task_id}
    jp = result.get("job_payload")
    if not isinstance(jp, dict):
        return {"ok": False, "error": "missing job_payload"}
    _append(conn, task_id, EventType.JOB_QUEUED, jp)
    return {"ok": True, "task_id": task_id, "plan_id": result.get("plan_id")}


def _handle_record_verification_result(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    task_id = str(env.task_id or env.payload.get("task_id") or "").strip()
    if not task_id or not task_exists(conn, task_id):
        return {"ok": False, "error": "task_id required"}
    payload = {
        "plan_id": str(env.payload.get("plan_id") or ""),
        "step_id": str(env.payload.get("step_id") or ""),
        "verification_id": str(env.payload.get("verification_id") or ""),
        "verdict": str(env.payload.get("verdict") or ""),
        "summary": str(env.payload.get("summary") or ""),
        "method": str(env.payload.get("method") or ""),
    }
    _append(conn, task_id, EventType.VERIFICATION_RECORDED, payload)
    return {"ok": True, "task_id": task_id}


def append_internal_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: EventType,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if not task_exists(conn, task_id):
        return {"ok": False, "error": f"unknown task_id: {task_id}"}
    seq = _append(conn, task_id, event_type, payload)
    return {"ok": True, "task_id": task_id, "seq": seq}
