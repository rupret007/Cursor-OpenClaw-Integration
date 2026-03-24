"""Autonomous optimization loop for Andrea orchestration quality."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .kill_switch import kill_switch_status
from .observability import metric_log, structured_log
from .policy import (
    META_DIGEST_KEY,
    META_DIGEST_TS_KEY,
    evaluate_skill_absence_claim,
)
from .projector import project_task_dict
from .cursor_plan_execute import (
    TERMINAL_CURSOR_AGENT_STATUSES,
    build_self_heal_cursor_planner_prompt,
    enrich_handoff_payload_from_agent_status,
    executor_model_for_lane,
    extract_plan_text_from_conversation,
    fetch_agent_conversation_payload,
    plan_first_enabled,
    planner_model_for_lane,
    plan_text_usable,
    poll_cursor_agent_until_terminal,
    run_cursor_handoff_cli,
    self_heal_handoff_poll_params,
    self_heal_handoff_timeout_seconds,
)
from .repair_executor import (
    build_default_verification_checks,
    post_cursor_verification_enabled,
    verify_cursor_branch_in_isolated_worktree,
)
from .repair_policy import SAFE_REPAIR_ROOTS
from .schema import EventType
from .store import (
    SYSTEM_TASK_ID,
    append_event,
    ensure_system_task,
    get_latest_experience_run,
    load_events_for_task,
    list_tasks,
    set_meta,
    task_exists,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CURSOR_HANDOFF_SCRIPT = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"
CAPABILITY_SCRIPT = REPO_ROOT / "scripts" / "andrea_capabilities.py"
DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
SAFE_AUTO_HEAL_ROOTS = (
    *SAFE_REPAIR_ROOTS,
)
OPENCLAW_HYBRID_SCRIPT = REPO_ROOT / "scripts" / "andrea_sync_openclaw_hybrid.py"

BREW_FORMULA_OVERRIDES: Dict[str, str] = {
    "apple-notes": "memo",
    "apple-reminders": "steipete/tap/remindctl",
    "memo": "memo",
    "remindctl": "steipete/tap/remindctl",
    "rg": "ripgrep",
    "session-logs": "jq",
    "tmux": "tmux",
}

CONFIG_VALUE_REPAIRS: Dict[str, Any] = {
    "plugins.entries.voice-call.enabled": True,
}


CATEGORY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "overdelegation": {
        "title": "Tighten direct-vs-delegate routing",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/telegram_continuation.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent tasks show lightweight/meta turns still being treated like delegated work.",
        "action": "Refine router and continuation heuristics so direct Andrea answers win more reliably.",
    },
    "visibility_noise": {
        "title": "Reduce low-value collaboration visibility",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/server.py",
            "services/andrea_sync/telegram_format.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent tasks requested full visibility or showed visible collaboration without enough collaborative value.",
        "action": "Tighten visibility escalation and progress-copy rules so full dialogue remains meaningful.",
    },
    "continuation_risk": {
        "title": "Harden Telegram continuation boundaries",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/telegram_continuation.py",
            "services/andrea_sync/server.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent tasks indicate heavy continuation attachment or thread-merging risk.",
        "action": "Favor explicit reply threading and reduce timing-only continuation behavior.",
    },
    "execution_failure": {
        "title": "Reduce delegated execution failures",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent delegated runs failed before producing a trustworthy answer.",
        "action": "Improve execution fallback, error handling, and lane selection before escalating to the user.",
    },
    "blocked_capability": {
        "title": "Harden blocked collaboration fallback UX",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "skills/cursor_handoff/SKILL.md",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent collaboration attempts hit internal capability limits and needed a calmer fallback contract.",
        "action": "Keep exact runtime diagnostics internal while giving the user a product-level limitation or fallback explanation.",
    },
    "runtime_leakage": {
        "title": "Stop runtime jargon from reaching users",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/telegram_format.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent user-visible replies leaked internal runtime, session, or tool mechanics.",
        "action": "Route exact diagnostics into internal traces and keep user-facing summaries product-level.",
    },
    "planner_failure": {
        "title": "Stabilize planning phase failures",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "services/andrea_sync/schema.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent orchestration runs failed before the planning phase produced a stable handoff plan.",
        "action": "Harden planning outputs, phase tracking, and fallbacks before escalating or replying.",
    },
    "critic_failure": {
        "title": "Strengthen critique and verification",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "services/andrea_sync/schema.py",
            "tests/test_andrea_sync_openclaw_hybrid.py",
        ],
        "problem": "Recent collaborative runs missed or failed the critique pass that should catch weak plans before execution.",
        "action": "Make critique outputs explicit, auditable, and required when collaboration is requested.",
    },
    "collaboration_usefulness": {
        "title": "Measure and reduce wasted task-path collaboration",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/collaboration_runtime.py",
            "services/andrea_sync/activation_policy.py",
            "services/andrea_sync/collaboration_effectiveness.py",
            "services/andrea_sync/plan_runtime.py",
            "services/andrea_sync/dashboard.py",
            "services/andrea_sync/experience_assurance.py",
            "tests/test_collaboration_runtime.py",
        ],
        "problem": "Advisory collaboration rounds add latency; many runs may be informational or wasteful without changing the safe next action.",
        "action": "Use activation + outcome ledgers, scenario profiles, and shadow adaptive policy before promoting live advisory or bounded actions.",
    },
    "executor_failure": {
        "title": "Reduce execution lane regressions",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "skills/cursor_handoff/SKILL.md",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent orchestration runs failed during the execution phase after planning succeeded.",
        "action": "Tighten Cursor handoff, execution retries, and synthesis after execution finishes.",
    },
    "proactive_delivery": {
        "title": "Harden proactive reminder delivery",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/bus.py",
            "services/andrea_sync/server.py",
            "services/andrea_sync/store.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent proactive follow-through attempts were created but not delivered cleanly.",
        "action": "Keep reminders quiet but reliable by improving delivery-target resolution and reminder sweep safeguards.",
    },
    "user_dissatisfaction": {
        "title": "Investigate recent negative user outcomes",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/server.py",
            "services/andrea_sync/telegram_format.py",
        ],
        "problem": "Recent task feedback suggests the experience felt wrong even when the task completed.",
        "action": "Review routing, visibility, and wording decisions for the negative-feedback examples.",
    },
}


def _clip(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _run_subprocess(argv: List[str], *, cwd: Path | None = None) -> Dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd or REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "argv": list(argv),
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "ok": proc.returncode == 0,
    }


def _run_json_command(argv: List[str], *, cwd: Path | None = None) -> Dict[str, Any]:
    result = _run_subprocess(argv, cwd=cwd)
    blob = (result.get("stdout") or "").strip()
    if not result["ok"]:
        return {
            "ok": False,
            "error": _clip(result.get("stderr") or blob or "command failed", 500),
            "command": result,
        }
    if not blob:
        return {"ok": False, "error": "command returned no JSON", "command": result}
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"command returned invalid JSON: {_clip(blob, 300)}",
            "command": result,
        }
    if not isinstance(payload, dict):
        return {"ok": False, "error": "command returned non-object JSON", "command": result}
    return {"ok": True, "payload": payload, "command": result}


def _openclaw_skill_info(skill_key: str) -> Dict[str, Any]:
    result = _run_json_command(["openclaw", "skills", "info", skill_key, "--json"])
    if not result.get("ok"):
        return {
            "ok": False,
            "skill_key": skill_key,
            "error": str(result.get("error") or "skills info failed"),
            "command": result.get("command"),
        }
    payload = dict(result.get("payload") or {})
    payload["ok"] = True
    payload["skill_key"] = str(payload.get("skillKey") or payload.get("name") or skill_key)
    payload["command"] = result.get("command")
    return payload


def _openclaw_config_path() -> Path:
    raw = os.environ.get("OPENCLAW_CONFIG_PATH") or str(DEFAULT_OPENCLAW_CONFIG)
    return Path(raw).expanduser()


def _load_openclaw_config() -> Dict[str, Any]:
    path = _openclaw_config_path()
    if not path.is_file():
        return {"ok": True, "path": str(path), "data": {}, "format": "json"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {"ok": True, "path": str(path), "data": data, "format": "json"}
    except json.JSONDecodeError:
        pass
    try:
        import json5  # type: ignore

        data = json5.loads(text)
        if isinstance(data, dict):
            return {"ok": True, "path": str(path), "data": data, "format": "json5"}
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": False,
        "path": str(path),
        "error": "openclaw config is not parseable as JSON/JSON5",
    }


def _set_dotted_path(root: Dict[str, Any], dotted_path: str, value: Any) -> bool:
    parts = [str(part).strip() for part in str(dotted_path or "").split(".") if str(part).strip()]
    if not parts:
        return False
    node: Dict[str, Any] = root
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    changed = node.get(parts[-1]) != value
    node[parts[-1]] = value
    return changed


def _save_openclaw_config(data: Dict[str, Any]) -> Dict[str, Any]:
    path = _openclaw_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}


def _repair_missing_config(skill_info: Dict[str, Any]) -> Dict[str, Any]:
    missing = skill_info.get("missing") if isinstance(skill_info.get("missing"), dict) else {}
    config_paths = missing.get("config") if isinstance(missing.get("config"), list) else []
    if not config_paths:
        return {"ok": True, "changed": False, "actions": [], "unsupported": []}
    loaded = _load_openclaw_config()
    if not loaded.get("ok"):
        return {
            "ok": False,
            "changed": False,
            "actions": [],
            "unsupported": list(config_paths),
            "error": str(loaded.get("error") or "config load failed"),
        }
    data = dict(loaded.get("data") or {})
    actions: List[Dict[str, Any]] = []
    unsupported: List[str] = []
    changed = False
    for config_path in config_paths:
        key = str(config_path or "").strip()
        if key not in CONFIG_VALUE_REPAIRS:
            unsupported.append(key)
            continue
        if _set_dotted_path(data, key, CONFIG_VALUE_REPAIRS[key]):
            changed = True
            actions.append(
                {
                    "kind": "config_repair",
                    "path": key,
                    "value": CONFIG_VALUE_REPAIRS[key],
                }
            )
    if changed:
        saved = _save_openclaw_config(data)
        if not saved.get("ok"):
            return {
                "ok": False,
                "changed": False,
                "actions": actions,
                "unsupported": unsupported,
                "error": str(saved.get("error") or "config save failed"),
            }
    return {"ok": True, "changed": changed, "actions": actions, "unsupported": unsupported}


def _install_command_from_spec(skill_key: str, spec: Dict[str, Any]) -> List[str]:
    kind = str(spec.get("kind") or "").strip().lower()
    bins = [str(v).strip() for v in (spec.get("bins") or []) if str(v).strip()]
    if kind == "brew":
        formula = str(spec.get("formula") or "").strip()
        if not formula:
            formula = BREW_FORMULA_OVERRIDES.get(skill_key) or ""
        if not formula and len(bins) == 1:
            formula = BREW_FORMULA_OVERRIDES.get(bins[0]) or bins[0]
        return ["brew", "install", formula] if formula else []
    if kind in {"node", "npm"}:
        package = str(spec.get("package") or "").strip()
        if not package and len(bins) == 1:
            package = bins[0]
        manager = str(os.environ.get("ANDREA_RUNTIME_SKILL_NODE_MANAGER") or "npm").strip().lower()
        if not package:
            return []
        if manager == "pnpm":
            return ["pnpm", "add", "-g", package]
        if manager == "yarn":
            return ["yarn", "global", "add", package]
        return ["npm", "install", "-g", package]
    return []


def _install_commands_for_skill(skill_key: str, skill_info: Dict[str, Any]) -> List[List[str]]:
    commands: List[List[str]] = []
    install_specs = skill_info.get("install") if isinstance(skill_info.get("install"), list) else []
    for raw in install_specs:
        if not isinstance(raw, dict):
            continue
        command = _install_command_from_spec(skill_key, raw)
        if command and command not in commands:
            commands.append(command)
    missing = skill_info.get("missing") if isinstance(skill_info.get("missing"), dict) else {}
    bins = missing.get("bins") if isinstance(missing.get("bins"), list) else []
    for raw in bins:
        bin_name = str(raw or "").strip()
        if not bin_name:
            continue
        command = ["brew", "install", BREW_FORMULA_OVERRIDES.get(bin_name) or bin_name]
        if command not in commands:
            commands.append(command)
    return commands


def _publish_capability_snapshot_direct(conn: sqlite3.Connection, *, actor: str) -> Dict[str, Any]:
    result = _run_json_command([sys.executable, str(CAPABILITY_SCRIPT), "--json"], cwd=REPO_ROOT)
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "capability publish failed")}
    payload = dict(result.get("payload") or {})
    payload["published_ts"] = time.time()
    set_meta(conn, META_DIGEST_KEY, json.dumps(payload, ensure_ascii=False))
    set_meta(conn, META_DIGEST_TS_KEY, str(payload["published_ts"]))
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    excerpt = json.dumps({"summary": payload.get("summary"), "row_count": len(rows)})[:480]
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.CAPABILITY_SNAPSHOT,
        {"summary_json_excerpt": excerpt, "channel": actor},
    )
    return {
        "ok": True,
        "published_ts": payload["published_ts"],
        "summary": payload.get("summary"),
        "rows": rows,
    }


def evaluate_background_readiness(
    conn: sqlite3.Connection,
    *,
    idle_seconds: float = 120.0,
    task_limit: int = 40,
) -> Dict[str, Any]:
    now = time.time()
    active_statuses = {"created", "queued", "running", "awaiting_approval"}
    active_task_ids: List[str] = []
    latest_user_work_age: float | None = None
    for row in list_tasks(conn, limit=max(1, int(task_limit))):
        task_id = str(row.get("task_id") or "")
        channel = str(row.get("channel") or "")
        if not task_id or task_id == SYSTEM_TASK_ID or channel == "internal":
            continue
        updated_at = row.get("updated_at")
        try:
            age = max(0.0, now - float(updated_at))
        except (TypeError, ValueError):
            age = None
        if age is not None:
            if latest_user_work_age is None or age < latest_user_work_age:
                latest_user_work_age = age
        status = str(row.get("status") or "").strip().lower()
        if status in active_statuses:
            active_task_ids.append(task_id)
    idle_ok = latest_user_work_age is None or latest_user_work_age >= float(idle_seconds)
    return {
        "ready": not active_task_ids and idle_ok,
        "active_task_ids": active_task_ids,
        "latest_user_work_age_seconds": latest_user_work_age,
        "idle_seconds_required": float(idle_seconds),
        "idle_ok": idle_ok,
    }


def _build_background_planner_prompt(
    findings: List[Dict[str, Any]], proposals: List[Dict[str, Any]]
) -> str:
    return (
        build_openclaw_optimization_prompt(findings, proposals)
        + "\nYou are the Gemini-first background planner.\n"
        "Produce a calm operator-grade plan that prioritizes trust, correctness, and self-healing opportunities.\n"
        "Call out the highest-leverage proposal first and keep the summary concise.\n"
    )


def _build_background_critique_prompt(
    planner_summary: str,
    proposals: List[Dict[str, Any]],
    *,
    lane_label: str,
) -> str:
    proposal_lines = "\n".join(
        f"- {row.get('proposal_id')}: {row.get('title')}" for row in proposals[:8]
    ) or "- no proposals generated"
    return (
        f"You are the {lane_label} critique lane for Andrea's background optimizer.\n"
        "Challenge the draft plan, point out hidden risk, and highlight anything that should be rejected or refined.\n\n"
        f"Planner summary:\n{planner_summary or '- no planner summary'}\n\n"
        f"Candidate proposals:\n{proposal_lines}\n"
    )


def _run_background_lane(
    *,
    prompt: str,
    preferred_model_family: str,
    preferred_model_label: str,
    repo_path: Path,
    task_id: str,
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(OPENCLAW_HYBRID_SCRIPT),
        "--task-id",
        task_id,
        "--prompt",
        prompt,
        "--repo",
        str(repo_path),
        "--agent-id",
        (os.environ.get("ANDREA_OPENCLAW_AGENT_ID") or "main").strip() or "main",
        "--session-id",
        f"andrea-opt-{preferred_model_family}-{uuid.uuid4().hex[:10]}",
        "--route-reason",
        "background_optimization",
        "--collaboration-mode",
        "collaborative",
        "--preferred-model-family",
        preferred_model_family,
        "--preferred-model-label",
        preferred_model_label,
        "--timeout-seconds",
        str(max(60, int(os.environ.get("ANDREA_OPENCLAW_TIMEOUT_SECONDS") or "900"))),
        "--thinking",
        str(os.environ.get("ANDREA_OPENCLAW_THINKING") or "medium"),
    ]
    result = _run_subprocess(cmd, cwd=repo_path)
    stdout = str(result.get("stdout") or "").strip()
    if not result.get("ok"):
        return {
            "ok": False,
            "requested_family": preferred_model_family,
            "requested_label": preferred_model_label,
            "error": _clip(result.get("stderr") or stdout or "lane failed", 500),
        }
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        return {
            "ok": False,
            "requested_family": preferred_model_family,
            "requested_label": preferred_model_label,
            "error": f"invalid lane JSON: {_clip(stdout, 300)}",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "requested_family": preferred_model_family,
            "requested_label": preferred_model_label,
            "error": "lane returned non-object JSON",
        }
    return {
        "ok": bool(payload.get("ok")),
        "requested_family": preferred_model_family,
        "requested_label": preferred_model_label,
        "summary": _clip(payload.get("summary") or payload.get("user_summary") or "", 800),
        "provider": str(payload.get("provider") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        "blocked_reason": _clip(payload.get("blocked_reason") or "", 400),
        "error": _clip(payload.get("error") or "", 400),
    }


def _run_background_analysis_lanes(
    conn: sqlite3.Connection,
    *,
    findings: List[Dict[str, Any]],
    proposals: List[Dict[str, Any]],
    repo_path: Path,
    actor: str,
    auto_apply_ready: bool,
) -> Dict[str, Any]:
    planner = _run_background_lane(
        prompt=_build_background_planner_prompt(findings, proposals),
        preferred_model_family="gemini",
        preferred_model_label="Gemini",
        repo_path=repo_path,
        task_id=SYSTEM_TASK_ID,
    )
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.ORCHESTRATION_STEP,
        {
            "phase": "plan",
            "status": "completed" if planner.get("ok") else "failed",
            "lane": "openclaw",
            "summary": str(planner.get("summary") or planner.get("error") or ""),
            "provider": str(planner.get("provider") or ""),
            "model": str(planner.get("model") or planner.get("requested_label") or ""),
        },
    )
    critiques: List[Dict[str, Any]] = []
    for family, label in (("minimax", "MiniMax"), ("openai", "OpenAI")):
        critique = _run_background_lane(
            prompt=_build_background_critique_prompt(
                str(planner.get("summary") or ""),
                proposals,
                lane_label=label,
            ),
            preferred_model_family=family,
            preferred_model_label=label,
            repo_path=repo_path,
            task_id=SYSTEM_TASK_ID,
        )
        critiques.append(critique)
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.ORCHESTRATION_STEP,
            {
                "phase": "critique",
                "status": "completed" if critique.get("ok") else "failed",
                "lane": "openclaw",
                "summary": str(critique.get("summary") or critique.get("error") or ""),
                "provider": str(critique.get("provider") or ""),
                "model": str(critique.get("model") or critique.get("requested_label") or ""),
            },
        )
    auto_heal: Dict[str, Any] = {"applied": [], "failed": []}
    if auto_apply_ready and proposals:
        auto_heal = apply_ready_proposals(
            conn,
            proposals=proposals,
            repo_path=repo_path,
            actor=actor,
            max_apply=1,
        )
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.ORCHESTRATION_STEP,
        {
            "phase": "execution",
            "status": "completed"
            if not auto_apply_ready or bool(auto_heal.get("applied"))
            else ("failed" if auto_heal.get("failed") else "completed"),
            "lane": "cursor" if auto_apply_ready else "openclaw",
            "summary": (
                f"Auto-applied {len(auto_heal.get('applied') or [])} proposal(s)."
                if auto_apply_ready
                else "Background execution lane was left in planning-only mode."
            ),
            "provider": "",
            "model": "Cursor" if auto_apply_ready else "",
        },
    )
    synthesis_summary = (
        str(planner.get("summary") or "")
        or "; ".join(
            str(item.get("summary") or "")
            for item in critiques
            if str(item.get("summary") or "").strip()
        )
        or "Background optimization cycle completed."
    )
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.ORCHESTRATION_STEP,
        {
            "phase": "synthesis",
            "status": "completed",
            "lane": "openclaw",
            "summary": synthesis_summary,
            "provider": str(planner.get("provider") or ""),
            "model": str(planner.get("model") or planner.get("requested_label") or ""),
        },
    )
    return {
        "planner": planner,
        "critiques": critiques,
        "auto_heal": auto_heal,
        "budget_usage": {
            "gemini_runs": 1,
            "minimax_runs": 1,
            "openai_runs": 1,
            "cursor_execution_runs": 1 if auto_apply_ready else 0,
        },
    }


def collect_recent_task_outcomes(
    conn: sqlite3.Connection, *, limit: int = 60
) -> List[Dict[str, Any]]:
    outcomes: List[Dict[str, Any]] = []
    for row in list_tasks(conn, limit=max(1, int(limit))):
        task_id = str(row.get("task_id") or "")
        channel = str(row.get("channel") or "")
        if not task_id or not channel or task_id == SYSTEM_TASK_ID or channel == "internal":
            continue
        proj = project_task_dict(conn, task_id, channel)
        meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
        outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
        outcomes.append(
            {
                "task_id": task_id,
                "channel": channel,
                "status": str(proj.get("status") or ""),
                "summary": str(proj.get("summary") or ""),
                "updated_at": row.get("updated_at"),
                "outcome": dict(outcome),
                "meta": meta,
            }
        )
    return outcomes


def detect_collaboration_policy_findings(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Surface evidence-backed collaboration policy signals as optimizer findings."""
    from .collaboration_effectiveness import rollup_collaboration_policy_profiles
    from .collaboration_promotion import (
        list_bounded_action_candidates,
        list_promotion_candidates,
        promotion_controller_enabled,
    )

    roll = rollup_collaboration_policy_profiles(conn)
    if not roll.get("ok"):
        return []
    out: List[Dict[str, Any]] = []
    for sig in roll.get("recommendation_signals") or []:
        subj = str(sig.get("subject") or "")
        samples = int(sig.get("evidence_samples") or 0)
        out.append(
            {
                "category": "collaboration_usefulness",
                "severity": "medium",
                "count": max(1, min(samples, 12)),
                "task_ids": [],
                "examples": [
                    {
                        "task_id": "",
                        "summary": subj[:200],
                        "result_kind": "collaboration_policy_signal",
                    }
                ],
            }
        )
    if promotion_controller_enabled():
        for cand in list_promotion_candidates(conn):
            sk = str(cand.get("subject_key") or "")
            out.append(
                {
                    "category": "collaboration_usefulness",
                    "severity": "low",
                    "count": 1,
                    "task_ids": [],
                    "examples": [
                        {
                            "task_id": "",
                            "summary": f"promotion_candidate_live_advisory:{sk}",
                            "result_kind": "collaboration_promotion_candidate",
                        }
                    ],
                }
            )
        for cand in list_bounded_action_candidates(conn):
            sk = str(cand.get("subject_key") or "")
            out.append(
                {
                    "category": "collaboration_usefulness",
                    "severity": "low",
                    "count": 1,
                    "task_ids": [],
                    "examples": [
                        {
                            "task_id": "",
                            "summary": f"promotion_candidate_bounded_action:{sk}",
                            "result_kind": "collaboration_bounded_promotion_candidate",
                        }
                    ],
                }
            )
    return out


def detect_failure_categories(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in outcomes:
        outcome = item.get("outcome") if isinstance(item.get("outcome"), dict) else {}
        flags = outcome.get("ux_flags") if isinstance(outcome.get("ux_flags"), list) else []
        task_id = str(item.get("task_id") or "")

        categories_for_task: List[str] = []
        if "overdelegated_meta_question" in flags:
            categories_for_task.append("overdelegation")
        if "low_value_full_visibility" in flags:
            categories_for_task.append("visibility_noise")
        if "continuation_heavy" in flags:
            categories_for_task.append("continuation_risk")
        if "execution_failed" in flags or str(outcome.get("terminal_status") or "") == "failed":
            categories_for_task.append("execution_failure")
        if "blocked_capability" in flags or "internal_runtime_trace" in flags:
            categories_for_task.append("blocked_capability")
        if "runtime_jargon_leaked" in flags:
            categories_for_task.append("runtime_leakage")
        if "planner_failure" in flags:
            categories_for_task.append("planner_failure")
        if "critic_failure" in flags or "critic_missing" in flags:
            categories_for_task.append("critic_failure")
        if "executor_failure" in flags:
            categories_for_task.append("executor_failure")
        if "proactive_delivery_failed" in flags:
            categories_for_task.append("proactive_delivery")
        if "negative_feedback" in flags:
            categories_for_task.append("user_dissatisfaction")

        for category in categories_for_task:
            bucket = buckets.setdefault(
                category,
                {
                    "category": category,
                    "severity": CATEGORY_TEMPLATES.get(category, {}).get("severity", "medium"),
                    "count": 0,
                    "task_ids": [],
                    "examples": [],
                },
            )
            bucket["count"] += 1
            if task_id and task_id not in bucket["task_ids"]:
                bucket["task_ids"].append(task_id)
            if len(bucket["examples"]) < 4:
                bucket["examples"].append(
                    {
                        "task_id": task_id,
                        "summary": _clip(item.get("summary"), 160),
                        "result_kind": str(outcome.get("result_kind") or ""),
                    }
                )
    return sorted(buckets.values(), key=lambda row: (-int(row["count"]), row["category"]))


def build_openclaw_optimization_prompt(
    findings: List[Dict[str, Any]], proposals: List[Dict[str, Any]]
) -> str:
    finding_lines = []
    for row in findings[:8]:
        finding_lines.append(
            f"- {row['category']}: count={row['count']} severity={row['severity']} tasks={', '.join(row['task_ids'][:5])}"
        )
    proposal_lines = []
    for row in proposals[:8]:
        proposal_lines.append(
            f"- {row['proposal_id']}: {row['title']} -> lane={row['preferred_execution_lane']} status={row['status']}"
        )
    findings_block = "\n".join(finding_lines) or "- no major recurring failures found"
    proposals_block = "\n".join(proposal_lines) or "- no proposals generated"
    return (
        "You are Andrea's optimization analyst.\n"
        "Review these recurring orchestration issues and the draft optimization proposals.\n"
        "Return a concise operator-facing analysis that prioritizes trust, calm UX, and correct lane selection.\n\n"
        "Findings:\n"
        f"{findings_block}\n\n"
        "Draft proposals:\n"
        f"{proposals_block}\n"
    )


def build_structured_proposals(
    findings: List[Dict[str, Any]], *, gate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for row in findings:
        template = CATEGORY_TEMPLATES.get(row["category"])
        if not template:
            continue
        proposal_id = f"prop_{uuid.uuid4().hex[:10]}"
        gate_allowed = bool(gate.get("allowed"))
        proposals.append(
            {
                "proposal_id": proposal_id,
                "title": template["title"],
                "category": row["category"],
                "severity": row["severity"],
                "status": "branch_prep_ready" if gate_allowed else "gated",
                "problem_statement": (
                    f"{template['problem']} Observed in {row['count']} recent task(s)."
                ),
                "recommended_action": template["action"],
                "target_files": list(template["target_files"]),
                "preferred_execution_lane": "cursor_branch_prep",
                "analysis_lane": "openclaw",
                "evidence_task_ids": list(row.get("task_ids") or [])[:8],
                "branch_prep_allowed": gate_allowed,
                "gate_reasons": list(gate.get("reasons") or []),
            }
        )
    return proposals


def build_background_regression_report(
    conn: sqlite3.Connection,
    *,
    max_age_seconds: float,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build regression_report for autonomy gate from latest persisted experience run.

    Background autonomy must not use synthetic passing regressions. Returns ``({}, meta)``
    when no usable run exists. When the latest run is older than ``max_age_seconds``,
    returns a failing report (``passed: False``) so the gate stays closed, but
    ``meta["fresh"]`` is False so callers can skip expensive background lanes.

    Meta keys:
      - source: ``none`` | ``experience_assurance``
      - run_id, total_checks, age_seconds, fresh, eligible_for_background_repair
      - verification_report: dict suitable for ``run_incident_repair_cycle`` when fresh
      - blocked_reason: optional string
    """
    now = time.time()
    run = get_latest_experience_run(conn)
    rid = str(run.get("run_id") or "").strip()
    meta: Dict[str, Any] = {
        "source": "experience_assurance",
        "run_id": rid,
        "fresh": False,
        "age_seconds": None,
        "total_checks": 0,
        "verification_report": None,
        "eligible_for_background_repair": False,
        "blocked_reason": "",
    }
    if not rid:
        meta["source"] = "none"
        meta["blocked_reason"] = "no_experience_run"
        return {}, meta

    checks = run.get("checks") if isinstance(run.get("checks"), list) else []
    total = int(run.get("total_checks") or 0) or len(checks)
    meta["total_checks"] = total
    if total <= 0:
        meta["blocked_reason"] = "experience_run_empty"
        meta["source"] = "none"
        return {}, meta

    ts = float(run.get("updated_at") or run.get("completed_at") or run.get("created_at") or 0.0)
    age = (now - ts) if ts > 0.0 else float("inf")
    meta["age_seconds"] = None if age == float("inf") else float(age)

    vr_in = run.get("verification_report")
    if isinstance(vr_in, dict) and vr_in:
        verification_report = dict(vr_in)
    else:
        verification_report = {
            "passed": bool(run.get("passed")),
            "summary": str(run.get("summary") or ""),
            "checks": checks,
            "metadata": {
                "run_id": rid,
                "actor": str(run.get("actor") or ""),
                "source": "experience_assurance",
            },
        }
    meta["verification_report"] = verification_report

    if age > float(max_age_seconds):
        meta["blocked_reason"] = "experience_run_stale"
        report = {
            "passed": False,
            "total": total,
            "command": "experience_assurance_stale",
            "run_id": rid,
        }
        return report, meta

    meta["fresh"] = True
    meta["eligible_for_background_repair"] = True
    passed = bool(run.get("passed"))
    report = {
        "passed": passed,
        "total": total,
        "command": "experience_assurance",
        "run_id": rid,
        "actor": str(run.get("actor") or ""),
    }
    return report, meta


def evaluate_autonomy_gate(
    conn: sqlite3.Connection,
    *,
    regression_report: Dict[str, Any] | None,
    required_skills: List[str] | None,
) -> Dict[str, Any]:
    ks = kill_switch_status(conn)
    report = regression_report if isinstance(regression_report, dict) else {}
    total = int(report.get("total") or 0)
    passed = bool(report.get("passed")) and total > 0
    skill_checks: List[Dict[str, Any]] = []
    reasons: List[str] = []
    allowed = True

    if ks.get("engaged"):
        allowed = False
        reasons.append("kill_switch_engaged")
    if not passed:
        allowed = False
        reasons.append("regression_report_missing_or_failed")

    for skill in required_skills or []:
        verdict = evaluate_skill_absence_claim(conn, str(skill), max_age_seconds=900.0)
        skill_checks.append({"skill": str(skill), **verdict})
        if verdict.get("must_refresh"):
            allowed = False
            reasons.append(f"capability_digest_refresh_required:{skill}")

    return {
        "allowed": allowed,
        "kill_switch_engaged": bool(ks.get("engaged")),
        "regression_report": {"passed": passed, "total": total},
        "skill_checks": skill_checks,
        "reasons": reasons,
    }


def record_regression_report(
    conn: sqlite3.Connection,
    report: Dict[str, Any] | None,
    *,
    actor: str = "internal",
) -> Dict[str, Any]:
    ensure_system_task(conn)
    payload = {
        "passed": bool((report or {}).get("passed")),
        "total": int((report or {}).get("total") or 0),
        "command": str((report or {}).get("command") or ""),
        "actor": actor,
    }
    append_event(conn, SYSTEM_TASK_ID, EventType.REGRESSION_RECORDED, payload)
    return payload


def heal_runtime_capability(
    conn: sqlite3.Connection,
    *,
    skill_key: str,
    install_slug: str = "",
    actor: str = "internal",
    allow_install: bool = True,
    allow_update_all: bool = True,
    allow_config_repair: bool = True,
) -> Dict[str, Any]:
    ensure_system_task(conn)
    target = str(skill_key or "").strip()
    if not target:
        return {"ok": False, "error": "skill_key required"}
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.CAPABILITY_HEAL_STARTED,
        {
            "skill_key": target,
            "install_slug": str(install_slug or target),
            "actor": actor,
        },
    )
    structured_log("capability_heal_started", skill_key=target, actor=actor)
    actions: List[Dict[str, Any]] = []
    blockers: List[str] = []
    info_before = _openclaw_skill_info(target)
    current = dict(info_before)

    def record_action(kind: str, result: Dict[str, Any], *, argv: List[str] | None = None) -> None:
        action = {"kind": kind, "ok": bool(result.get("ok"))}
        if argv:
            action["argv"] = list(argv)
        if result.get("returncode") is not None:
            action["returncode"] = int(result.get("returncode"))
        stderr = str(result.get("stderr") or "").strip()
        stdout = str(result.get("stdout") or "").strip()
        if stderr:
            action["stderr"] = _clip(stderr, 240)
        elif stdout:
            action["stdout"] = _clip(stdout, 240)
        actions.append(action)
        if not result.get("ok"):
            blockers.append(_clip(stderr or stdout or f"{kind} failed", 240))

    if not current.get("ok") and allow_install:
        install_target = str(install_slug or target)
        install_result = _run_subprocess(["openclaw", "skills", "install", install_target])
        record_action("openclaw_install", install_result, argv=install_result["argv"])
        current = _openclaw_skill_info(target)

    source = str(current.get("source") or "").strip().lower()
    if (
        current.get("ok")
        and not bool(current.get("eligible"))
        and allow_update_all
        and source
        and source != "openclaw-bundled"
    ):
        update_result = _run_subprocess(["openclaw", "skills", "update", "--all"])
        record_action("openclaw_update_all", update_result, argv=update_result["argv"])
        current = _openclaw_skill_info(target)

    if current.get("ok") and not bool(current.get("eligible")) and allow_config_repair:
        config_result = _repair_missing_config(current)
        if config_result.get("actions"):
            actions.extend(list(config_result.get("actions") or []))
        for path in config_result.get("unsupported") or []:
            blockers.append(f"unsupported_config:{path}")
        if not config_result.get("ok"):
            blockers.append(str(config_result.get("error") or "config repair failed"))
        if config_result.get("changed"):
            current = _openclaw_skill_info(target)

    if current.get("ok") and not bool(current.get("eligible")):
        commands = _install_commands_for_skill(target, current)
        for command in commands:
            install_result = _run_subprocess(command)
            record_action("dependency_install", install_result, argv=command)
        if commands:
            current = _openclaw_skill_info(target)

    snapshot = _publish_capability_snapshot_direct(conn, actor=actor)
    refresh_required = bool(actions)
    missing = current.get("missing") if isinstance(current.get("missing"), dict) else {}
    unresolved = {
        "bins": list(missing.get("bins") or []),
        "env": list(missing.get("env") or []),
        "config": list(missing.get("config") or []),
        "os": list(missing.get("os") or []),
    }
    result = {
        "ok": bool(current.get("ok")) and bool(current.get("eligible")),
        "skill_key": target,
        "install_slug": str(install_slug or target),
        "before": info_before,
        "after": current,
        "actions": actions,
        "blockers": blockers,
        "refresh_required": refresh_required,
        "snapshot": snapshot,
        "unresolved": unresolved,
    }
    if result["ok"]:
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.CAPABILITY_HEAL_COMPLETED,
            {
                "skill_key": target,
                "actor": actor,
                "action_count": len(actions),
                "refresh_required": refresh_required,
            },
        )
        metric_log(
            "capability_heal_completed",
            skill_key=target,
            actor=actor,
            action_count=len(actions),
            refresh_required=refresh_required,
        )
        structured_log(
            "capability_heal_completed",
            skill_key=target,
            actor=actor,
            action_count=len(actions),
            refresh_required=refresh_required,
        )
        return result
    error = "; ".join(
        item
        for item in blockers
        + [f"missing_bins:{','.join(unresolved['bins'])}" if unresolved["bins"] else ""]
        + [f"missing_env:{','.join(unresolved['env'])}" if unresolved["env"] else ""]
        + [f"missing_config:{','.join(unresolved['config'])}" if unresolved["config"] else ""]
        + [f"missing_os:{','.join(unresolved['os'])}" if unresolved["os"] else ""]
        if item
    ) or str(current.get("error") or "capability repair incomplete")
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.CAPABILITY_HEAL_FAILED,
        {
            "skill_key": target,
            "actor": actor,
            "action_count": len(actions),
            "refresh_required": refresh_required,
            "error": error,
        },
    )
    metric_log("capability_heal_failed", skill_key=target, actor=actor)
    structured_log("capability_heal_failed", skill_key=target, actor=actor, error=error)
    result["error"] = error
    return result


def get_optimization_proposal(
    conn: sqlite3.Connection, proposal_id: str
) -> Dict[str, Any]:
    if not task_exists(conn, SYSTEM_TASK_ID):
        return {}
    target = str(proposal_id or "").strip()
    if not target:
        return {}
    for _seq, _ts, et, payload in reversed(load_events_for_task(conn, SYSTEM_TASK_ID)):
        if et != EventType.OPTIMIZATION_PROPOSAL.value:
            continue
        if str(payload.get("proposal_id") or "").strip() == target:
            return dict(payload)
    return {}


def _normalize_target_files(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _proposal_apply_gate(proposal: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    allowed = True
    if not bool(proposal.get("branch_prep_allowed")):
        allowed = False
        reasons.append("branch_prep_not_allowed")
    status = str(proposal.get("status") or "").strip()
    if status not in {"branch_prep_ready", "approved", "proposed"}:
        allowed = False
        reasons.append(f"unsupported_status:{status or 'unknown'}")
    target_files = _normalize_target_files(proposal.get("target_files"))
    if len(target_files) > 12:
        allowed = False
        reasons.append("too_many_target_files")
    for path in target_files:
        if not any(path.startswith(prefix) for prefix in SAFE_AUTO_HEAL_ROOTS):
            allowed = False
            reasons.append(f"disallowed_target:{path}")
    if any(secret in " ".join(target_files).lower() for secret in (".env", "credentials", "secret", "token")):
        allowed = False
        reasons.append("sensitive_target_path")
    return {"allowed": allowed, "reasons": reasons, "target_files": target_files}


def _build_auto_heal_prompt(proposal: Dict[str, Any]) -> str:
    target_files = _normalize_target_files(proposal.get("target_files"))
    files_block = "\n".join(f"- {path}" for path in target_files[:12]) or "- no explicit file list"
    evidence_block = "\n".join(
        f"- {task_id}" for task_id in (proposal.get("evidence_task_ids") or [])[:8]
    ) or "- no task ids recorded"
    return (
        "You are Andrea's closed-loop local self-healing runner.\n"
        "Apply a safe, minimal local fix for this optimization proposal.\n"
        "Stay within the target files when possible. Do not touch secrets, env files, or unrelated areas.\n"
        "Prefer surgical fixes plus tests. Summarize what changed, what you verified, and any residual risk.\n\n"
        f"Proposal: {proposal.get('title')}\n"
        f"Category: {proposal.get('category')}\n"
        f"Problem: {proposal.get('problem_statement')}\n"
        f"Recommended action: {proposal.get('recommended_action')}\n"
        "Target files:\n"
        f"{files_block}\n\n"
        "Evidence task ids:\n"
        f"{evidence_block}\n"
    )


def _run_cursor_handoff_prompt(
    *,
    repo_path: Path,
    prompt: str,
    branch: str,
    cursor_mode: str,
    model: str | None = None,
) -> Dict[str, Any]:
    em = (model or "").strip() or executor_model_for_lane("self_heal")
    cmd = [
        sys.executable,
        str(CURSOR_HANDOFF_SCRIPT),
        "--repo",
        str(repo_path),
        "--prompt",
        prompt,
        "--mode",
        cursor_mode,
        "--read-only",
        "false",
        "--model",
        em,
        "--json",
        "--poll-max-attempts",
        "0",
        "--cli-timeout-seconds",
        "0",
        "--branch",
        branch,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.strip()
    payload: Dict[str, Any]
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"cursor_handoff returned invalid JSON: {stdout[:400]}") from exc
    else:
        payload = {}
    if proc.returncode != 0 or not payload.get("ok"):
        detail = str(payload.get("error") or payload.get("response") or proc.stderr or stdout[:400])
        raise RuntimeError(detail)
    payload["cursor_strategy"] = str(payload.get("cursor_strategy") or "single_pass")
    payload["executor_model"] = str(payload.get("executor_model") or em)
    return payload


def _try_self_heal_plan_first(
    *,
    repo_path: Path,
    executor_prompt: str,
    branch: str,
    cursor_mode: str,
) -> Dict[str, Any] | None:
    """Planner read-only pass then executor; None triggers single-pass fallback."""
    if not plan_first_enabled("self_heal"):
        return None
    pm = planner_model_for_lane("self_heal")
    if not pm:
        return None
    em = executor_model_for_lane("self_heal")
    poll_max, poll_iv = self_heal_handoff_poll_params()
    timeout_sec = self_heal_handoff_timeout_seconds()
    branch_plan = f"{branch}-plan-{uuid.uuid4().hex[:8]}"
    planner_prompt = build_self_heal_cursor_planner_prompt(executor_prompt)
    r1 = run_cursor_handoff_cli(
        repo_path=repo_path,
        prompt=planner_prompt,
        branch=branch_plan,
        cursor_mode=cursor_mode,
        read_only=True,
        model=pm,
        poll_max_attempts=poll_max,
        poll_interval_seconds=poll_iv,
        timeout_seconds=timeout_sec,
    )
    p1 = r1.get("payload") if isinstance(r1.get("payload"), dict) else {}
    if not r1.get("ok") or not p1.get("ok"):
        return None
    planner_agent = str(p1.get("agent_id") or "").strip()
    if not planner_agent:
        return None
    conv = fetch_agent_conversation_payload(
        repo_path=repo_path,
        agent_id=planner_agent,
        timeout_seconds=min(timeout_sec, 120),
    )
    resp = conv.get("response") if conv.get("ok") else None
    plan_text = extract_plan_text_from_conversation(resp) if resp is not None else ""
    if not plan_text_usable(plan_text):
        return None
    full_prompt = (
        f"{executor_prompt}\n\n## Cursor planner output\n\n{plan_text}\n\n"
        "Apply minimal, safe changes following the plan above.\n"
    )
    r2 = run_cursor_handoff_cli(
        repo_path=repo_path,
        prompt=full_prompt,
        branch=branch,
        cursor_mode=cursor_mode,
        read_only=False,
        model=em,
        poll_max_attempts=poll_max,
        poll_interval_seconds=poll_iv,
        timeout_seconds=timeout_sec,
    )
    p2 = r2.get("payload") if isinstance(r2.get("payload"), dict) else {}
    stdout2 = str(r2.get("stdout") or "").strip()
    if not r2.get("ok") or not p2.get("ok"):
        detail = str(
            p2.get("error") or p2.get("response") or r2.get("stderr") or stdout2 or "cursor executor failed"
        )
        raise RuntimeError(detail)
    out = dict(p2)
    out["cursor_strategy"] = "plan_first"
    out["planner_model"] = pm
    out["executor_model"] = em
    out["planner_agent_id"] = planner_agent
    out["planner_branch"] = branch_plan
    out["planner_status"] = str(p1.get("status") or "")
    out["plan_summary"] = _clip(plan_text, 800)
    return out


def _run_self_heal_cursor_handoff(
    *,
    repo_path: Path,
    prompt: str,
    branch: str,
    cursor_mode: str,
) -> Dict[str, Any]:
    planned = _try_self_heal_plan_first(
        repo_path=repo_path,
        executor_prompt=prompt,
        branch=branch,
        cursor_mode=cursor_mode,
    )
    if planned is not None:
        return planned
    return _run_cursor_handoff_prompt(
        repo_path=repo_path,
        prompt=prompt,
        branch=branch,
        cursor_mode=cursor_mode,
    )


def _self_heal_post_cursor_verify_enabled() -> bool:
    raw = os.environ.get("ANDREA_SELF_HEAL_POST_CURSOR_VERIFY")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    return post_cursor_verification_enabled()


def apply_optimization_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_payload: Dict[str, Any],
    repo_path: Path,
    actor: str = "internal",
) -> Dict[str, Any]:
    ensure_system_task(conn)
    proposal = dict(proposal_payload)
    if proposal.get("proposal_id") and not proposal.get("title"):
        proposal = {**get_optimization_proposal(conn, str(proposal.get("proposal_id"))), **proposal}
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "error": "proposal_id required"}
    gate = _proposal_apply_gate(proposal)
    branch = f"openclaw/autoheal-{proposal_id[:10]}"
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.LOCAL_AUTO_HEAL_STARTED,
        {
            "proposal_id": proposal_id,
            "title": str(proposal.get("title") or ""),
            "category": str(proposal.get("category") or ""),
            "actor": actor,
            "branch": branch,
        },
    )
    if not gate["allowed"]:
        error = ", ".join(gate["reasons"]) or "gate_blocked"
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_FAILED,
            {
                "proposal_id": proposal_id,
                "title": str(proposal.get("title") or ""),
                "category": str(proposal.get("category") or ""),
                "actor": actor,
                "branch": branch,
                "error": error,
            },
        )
        return {
            "ok": False,
            "proposal_id": proposal_id,
            "error": error,
            "gate": gate,
        }
    cursor_mode = (
        str(proposal.get("cursor_mode") or "").strip()
        or os.environ.get("ANDREA_SELF_HEAL_CURSOR_MODE", "").strip()
        or "auto"
    )
    try:
        payload = _run_self_heal_cursor_handoff(
            repo_path=repo_path,
            prompt=_build_auto_heal_prompt(proposal),
            branch=branch,
            cursor_mode=cursor_mode,
        )
    except Exception as exc:  # noqa: BLE001
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_FAILED,
            {
                "proposal_id": proposal_id,
                "title": str(proposal.get("title") or ""),
                "category": str(proposal.get("category") or ""),
                "actor": actor,
                "branch": branch,
                "error": str(exc),
                "submission_status": "failed",
                "verification_status": "not_attempted",
                "next_action": "retry_cursor_handoff_or_human",
            },
        )
        return {"ok": False, "proposal_id": proposal_id, "error": str(exc), "gate": gate}

    def _fail_auto_heal(
        error: str,
        *,
        submission_status: str,
        terminal_cursor_status: str,
        verification_status: str,
        next_action: str,
        can_auto_verify: bool | None = None,
        post_verify_error: str = "",
        ref_source: str = "",
        post_cursor_verification: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_FAILED,
            {
                "proposal_id": proposal_id,
                "title": str(proposal.get("title") or ""),
                "category": str(proposal.get("category") or ""),
                "actor": actor,
                "branch": str(payload.get("branch") or branch),
                "backend": str(payload.get("backend") or ""),
                "agent_id": str(payload.get("agent_id") or ""),
                "agent_url": str(payload.get("agent_url") or ""),
                "pr_url": str(payload.get("pr_url") or ""),
                "status": str(payload.get("status") or ""),
                "error": _clip(error, 800),
                "submission_status": submission_status,
                "terminal_cursor_status": terminal_cursor_status,
                "verification_status": verification_status,
                "next_action": next_action,
                "can_auto_verify": can_auto_verify,
                "post_verify_error": _clip(post_verify_error, 400),
                "ref_source": ref_source,
                "cursor_strategy": str(payload.get("cursor_strategy") or ""),
                "planner_model": str(payload.get("planner_model") or ""),
                "executor_model": str(payload.get("executor_model") or ""),
                "planner_agent_id": str(payload.get("planner_agent_id") or ""),
                "plan_summary": _clip(str(payload.get("plan_summary") or ""), 400),
                "post_cursor_verification": dict(post_cursor_verification or {}),
            },
        )
        return {
            "ok": False,
            "proposal_id": proposal_id,
            "error": error,
            "gate": gate,
            "branch": str(payload.get("branch") or branch),
            "backend": str(payload.get("backend") or ""),
            "agent_id": str(payload.get("agent_id") or ""),
            "agent_url": str(payload.get("agent_url") or ""),
            "pr_url": str(payload.get("pr_url") or ""),
            "status": str(payload.get("status") or ""),
            "submission_status": submission_status,
            "terminal_cursor_status": terminal_cursor_status,
            "verification_status": verification_status,
            "next_action": next_action,
            "post_cursor_verification": dict(post_cursor_verification or {}),
        }

    handoff = dict(payload)
    backend = str(handoff.get("backend") or "").lower()
    branch_name = str(handoff.get("branch") or branch).strip()
    agent_id = str(handoff.get("agent_id") or "").strip()
    poll_max, poll_iv = self_heal_handoff_poll_params()
    status_timeout = min(120, self_heal_handoff_timeout_seconds())
    terminal_up = str(handoff.get("status") or "").strip().upper()

    if agent_id:
        terminal_up, last_resp = poll_cursor_agent_until_terminal(
            repo_path=repo_path,
            agent_id=agent_id,
            max_attempts=poll_max,
            interval_seconds=poll_iv,
            status_timeout_seconds=status_timeout,
        )
        enrich_handoff_payload_from_agent_status(handoff, last_resp)
        handoff["status"] = terminal_up or str(handoff.get("status") or "")
        terminal_up = str(handoff.get("status") or "").strip().upper()
    elif backend == "api":
        return _fail_auto_heal(
            "cursor_handoff_missing_agent_id",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="not_attempted",
            next_action="retry_cursor_handoff_or_human",
            can_auto_verify=False,
        )

    if agent_id and terminal_up not in TERMINAL_CURSOR_AGENT_STATUSES:
        return _fail_auto_heal(
            "cursor_agent_not_terminal_after_poll",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up or "UNKNOWN",
            verification_status="not_attempted",
            next_action="monitor_cursor_or_verify_manually",
            can_auto_verify=False,
        )

    if agent_id and terminal_up in TERMINAL_CURSOR_AGENT_STATUSES and terminal_up != "FINISHED":
        return _fail_auto_heal(
            f"cursor_agent_ended_{terminal_up.lower() or 'unknown'}",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="not_attempted",
            next_action="human_review_cursor_failed",
            can_auto_verify=False,
        )

    if not _self_heal_post_cursor_verify_enabled():
        return _fail_auto_heal(
            "self_heal_post_cursor_verify_disabled",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="skipped",
            next_action="enable_post_verify_or_verify_manually",
            can_auto_verify=None,
            post_verify_error="disabled_by_env",
        )

    if not branch_name:
        return _fail_auto_heal(
            "self_heal_missing_branch_for_verify",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="unverified",
            next_action="human_review_branch_unavailable",
            can_auto_verify=False,
            post_verify_error="missing_branch",
        )

    should_verify = True
    if backend == "api":
        should_verify = terminal_up == "FINISHED"
    if not should_verify:
        return _fail_auto_heal(
            "self_heal_api_agent_not_finished",
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="skipped",
            next_action="monitor_cursor_or_verify_manually",
            can_auto_verify=False,
            post_verify_error="agent_not_finished",
        )

    checks = build_default_verification_checks(repo_path)
    vres = verify_cursor_branch_in_isolated_worktree(
        repo_path=repo_path,
        branch=branch_name,
        incident_id=f"autoheal-{proposal_id}",
        verification_checks=checks,
    )
    if vres.get("ok") and vres.get("passed"):
        result = {
            "ok": True,
            "proposal_id": proposal_id,
            "title": str(proposal.get("title") or ""),
            "category": str(proposal.get("category") or ""),
            "branch": branch_name,
            "backend": backend,
            "agent_id": agent_id,
            "agent_url": str(handoff.get("agent_url") or ""),
            "pr_url": str(handoff.get("pr_url") or ""),
            "status": terminal_up or str(handoff.get("status") or ""),
            "gate": gate,
            "submission_status": "succeeded",
            "terminal_cursor_status": terminal_up,
            "verification_status": "passed",
            "next_action": "none",
            "can_auto_verify": True,
            "post_cursor_verification": dict(vres),
        }
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_COMPLETED,
            {
                "proposal_id": proposal_id,
                "title": result["title"],
                "category": result["category"],
                "actor": actor,
                "branch": result["branch"],
                "backend": result["backend"],
                "agent_id": result["agent_id"],
                "agent_url": result["agent_url"],
                "pr_url": result["pr_url"],
                "status": result["status"],
                "submission_status": result["submission_status"],
                "terminal_cursor_status": result["terminal_cursor_status"],
                "verification_status": result["verification_status"],
                "next_action": result["next_action"],
                "can_auto_verify": result["can_auto_verify"],
                "ref_source": str(vres.get("ref_source") or ""),
                "cursor_strategy": str(handoff.get("cursor_strategy") or ""),
                "planner_model": str(handoff.get("planner_model") or ""),
                "executor_model": str(handoff.get("executor_model") or ""),
                "planner_agent_id": str(handoff.get("planner_agent_id") or ""),
                "plan_summary": _clip(str(handoff.get("plan_summary") or ""), 400),
                "post_cursor_verification": dict(vres),
            },
        )
        return result

    if vres.get("ok") and not vres.get("passed"):
        return _fail_auto_heal(
            str(vres.get("error") or "post_cursor_verification_failed"),
            submission_status="succeeded",
            terminal_cursor_status=terminal_up,
            verification_status="failed",
            next_action="human_review_verification_failed",
            can_auto_verify=True,
            post_verify_error=str(vres.get("error") or ""),
            ref_source=str(vres.get("ref_source") or ""),
            post_cursor_verification=vres,
        )

    return _fail_auto_heal(
        str(vres.get("error") or "post_cursor_verification_unavailable"),
        submission_status="succeeded",
        terminal_cursor_status=terminal_up,
        verification_status="unverified",
        next_action="human_review_branch_unavailable",
        can_auto_verify=False,
        post_verify_error=str(vres.get("error") or ""),
        ref_source=str(vres.get("ref_source") or ""),
        post_cursor_verification=vres,
    )


def apply_ready_proposals(
    conn: sqlite3.Connection,
    *,
    proposals: List[Dict[str, Any]] | None,
    repo_path: Path,
    actor: str = "internal",
    max_apply: int = 1,
) -> Dict[str, Any]:
    selected = [
        dict(row)
        for row in (proposals or [])
        if bool(row.get("branch_prep_allowed"))
        and str(row.get("status") or "").strip() == "branch_prep_ready"
    ][: max(1, int(max_apply))]
    results = [
        apply_optimization_proposal(
            conn,
            proposal_payload=row,
            repo_path=repo_path,
            actor=actor,
        )
        for row in selected
    ]
    return {
        "applied": [row for row in results if row.get("ok")],
        "failed": [row for row in results if not row.get("ok")],
    }


def run_optimization_cycle(
    conn: sqlite3.Connection,
    *,
    limit: int = 60,
    regression_report: Dict[str, Any] | None = None,
    required_skills: List[str] | None = None,
    emit_proposals: bool = True,
    actor: str = "internal",
    analysis_mode: str = "heuristic",
    repo_path: Path | None = None,
    auto_apply_ready: bool = False,
    idle_seconds: float = 120.0,
    autonomy_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    ensure_system_task(conn)
    run_id = f"opt_{uuid.uuid4().hex[:12]}"
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.OPTIMIZATION_RUN_STARTED,
        {
            "run_id": run_id,
            "actor": actor,
            "limit": int(limit),
            "analysis_mode": analysis_mode,
            "started_ts": time.time(),
        },
    )
    structured_log(
        "optimization_cycle_started",
        run_id=run_id,
        actor=actor,
        limit=limit,
        analysis_mode=analysis_mode,
    )
    try:
        readiness: Dict[str, Any] = {}
        if analysis_mode == "gemini_background":
            readiness = evaluate_background_readiness(conn, idle_seconds=idle_seconds)
            if not readiness.get("ready"):
                append_event(
                    conn,
                    SYSTEM_TASK_ID,
                    EventType.OPTIMIZATION_RUN_COMPLETED,
                    {
                        "run_id": run_id,
                        "actor": actor,
                        "analysis_mode": analysis_mode,
                        "outcome_count": 0,
                        "finding_count": 0,
                        "proposal_count": 0,
                        "gate_allowed": False,
                        "gate_reasons": ["background_not_idle"],
                        "background_ready": False,
                    },
                )
                return {
                    "ok": True,
                    "task_id": SYSTEM_TASK_ID,
                    "run_id": run_id,
                    "skipped": True,
                    "skip_reason": "background_not_idle",
                    "readiness": readiness,
                    "findings": [],
                    "proposals": [],
                    "gate": {"allowed": False, "reasons": ["background_not_idle"]},
                }
        if isinstance(regression_report, dict) and regression_report:
            record_regression_report(conn, regression_report, actor=actor)
        outcomes = collect_recent_task_outcomes(conn, limit=limit)
        findings = detect_failure_categories(outcomes) + detect_collaboration_policy_findings(conn)
        gate = evaluate_autonomy_gate(
            conn,
            regression_report=regression_report,
            required_skills=required_skills,
        )
        proposals = build_structured_proposals(findings, gate=gate) if emit_proposals else []
        openclaw_prompt = build_openclaw_optimization_prompt(findings, proposals)

        for finding in findings:
            append_event(
                conn,
                SYSTEM_TASK_ID,
                EventType.EVALUATION_RECORDED,
                {
                    "run_id": run_id,
                    "category": finding["category"],
                    "severity": finding["severity"],
                    "summary": (
                        f"{finding['category']} appeared in {finding['count']} recent task(s)."
                    ),
                    "count": finding["count"],
                    "evidence_task_ids": finding.get("task_ids", []),
                    "recommended_action": CATEGORY_TEMPLATES.get(
                        finding["category"], {}
                    ).get("action", ""),
                },
            )
            metric_log(
                "optimization_finding_recorded",
                run_id=run_id,
                category=finding["category"],
                severity=finding["severity"],
                count=finding["count"],
            )

        for proposal in proposals:
            append_event(conn, SYSTEM_TASK_ID, EventType.OPTIMIZATION_PROPOSAL, proposal)
            metric_log(
                "optimization_proposal_created",
                run_id=run_id,
                category=proposal["category"],
                status=proposal["status"],
                branch_prep_allowed=proposal["branch_prep_allowed"],
            )

        lane_bundle: Dict[str, Any] = {}
        if analysis_mode == "gemini_background":
            lane_bundle = _run_background_analysis_lanes(
                conn,
                findings=findings,
                proposals=proposals,
                repo_path=Path(repo_path or REPO_ROOT).expanduser(),
                actor=actor,
                auto_apply_ready=auto_apply_ready,
            )
        completed_payload: Dict[str, Any] = {
            "run_id": run_id,
            "actor": actor,
            "analysis_mode": analysis_mode,
            "outcome_count": len(outcomes),
            "finding_count": len(findings),
            "proposal_count": len(proposals),
            "gate_allowed": bool(gate.get("allowed")),
            "gate_reasons": list(gate.get("reasons") or []),
            "background_ready": readiness.get("ready") if readiness else None,
            "background_budget_usage": lane_bundle.get("budget_usage") if lane_bundle else {},
            "background_auto_applied": len(lane_bundle.get("auto_heal", {}).get("applied") or [])
            if lane_bundle
            else 0,
        }
        if isinstance(autonomy_evidence, dict) and autonomy_evidence:
            completed_payload["autonomy_evidence"] = {
                "source": str(autonomy_evidence.get("source") or ""),
                "run_id": str(autonomy_evidence.get("run_id") or ""),
                "fresh": bool(autonomy_evidence.get("fresh")),
                "age_seconds": autonomy_evidence.get("age_seconds"),
                "total_checks": int(autonomy_evidence.get("total_checks") or 0),
                "eligible_for_background_repair": bool(
                    autonomy_evidence.get("eligible_for_background_repair")
                ),
                "blocked_reason": str(autonomy_evidence.get("blocked_reason") or ""),
            }
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.OPTIMIZATION_RUN_COMPLETED,
            completed_payload,
        )
        metric_log(
            "optimization_cycle_completed",
            run_id=run_id,
            outcomes=len(outcomes),
            findings=len(findings),
            proposals=len(proposals),
            gate_allowed=bool(gate.get("allowed")),
        )
        structured_log(
            "optimization_cycle_completed",
            run_id=run_id,
            outcomes=len(outcomes),
            findings=len(findings),
            proposals=len(proposals),
            gate_allowed=bool(gate.get("allowed")),
        )
        result = {
            "ok": True,
            "task_id": SYSTEM_TASK_ID,
            "run_id": run_id,
            "outcomes_analyzed": len(outcomes),
            "findings": findings,
            "proposals": proposals,
            "gate": gate,
        }
        if readiness:
            result["readiness"] = readiness
        if lane_bundle:
            result["analysis_lanes"] = {
                "planner": lane_bundle.get("planner"),
                "critiques": lane_bundle.get("critiques"),
            }
            result["budget_usage"] = lane_bundle.get("budget_usage")
            result["auto_heal"] = lane_bundle.get("auto_heal")
        if isinstance(autonomy_evidence, dict) and autonomy_evidence:
            result["autonomy_evidence"] = dict(autonomy_evidence)
        if analysis_mode != "heuristic":
            result["openclaw_analysis_prompt"] = openclaw_prompt
        return result
    except Exception as exc:  # noqa: BLE001
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.OPTIMIZATION_RUN_FAILED,
            {
                "run_id": run_id,
                "actor": actor,
                "analysis_mode": analysis_mode,
                "error": str(exc),
            },
        )
        metric_log("optimization_cycle_failed", run_id=run_id)
        structured_log("optimization_cycle_failed", run_id=run_id, error=str(exc))
        return {
            "ok": False,
            "task_id": SYSTEM_TASK_ID,
            "run_id": run_id,
            "error": str(exc),
        }
