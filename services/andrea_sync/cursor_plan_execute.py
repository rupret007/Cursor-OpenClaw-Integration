"""
Cursor plan-first policy and low-level handoff helpers.

`--mode` on cursor_handoff.py selects API vs CLI transport. Model selection is separate
(`--model` / CURSOR_HANDOFF_MODEL). This module centralizes env-driven plan-first toggles
and subprocess calls so repair, optimizer, and Telegram paths can share behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
CURSOR_HANDOFF_SCRIPT = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"
CURSOR_OPENCLAW_SCRIPT = REPO_ROOT / "scripts" / "cursor_openclaw.py"

CursorLane = Literal["repair", "self_heal", "telegram"]

# Align with skills/cursor_handoff/scripts/cursor_handoff.py TERMINAL_STATUSES
TERMINAL_CURSOR_AGENT_STATUSES = frozenset({"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"})


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def plan_first_enabled(lane: CursorLane) -> bool:
    """True when this lane should run planner agent then executor agent."""
    keys = {
        "repair": "ANDREA_REPAIR_CURSOR_PLAN_FIRST",
        "self_heal": "ANDREA_SELF_HEAL_CURSOR_PLAN_FIRST",
        "telegram": "ANDREA_TELEGRAM_CURSOR_PLAN_FIRST",
    }
    raw = os.environ.get(keys[lane], "")
    if str(raw).strip() != "":
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    return _env_truthy("ANDREA_CURSOR_PLAN_FIRST_ENABLED", False)


def planner_model_for_lane(lane: CursorLane) -> str:
    overrides = {
        "repair": "ANDREA_REPAIR_CURSOR_PLANNER_MODEL",
        "self_heal": "ANDREA_SELF_HEAL_CURSOR_PLANNER_MODEL",
        "telegram": "ANDREA_TELEGRAM_CURSOR_PLANNER_MODEL",
    }
    v = os.environ.get(overrides[lane], "").strip()
    if v:
        return v
    return os.environ.get("ANDREA_CURSOR_PLANNER_MODEL", "").strip()


def executor_model_for_lane(lane: CursorLane) -> str:
    overrides = {
        "repair": "ANDREA_REPAIR_CURSOR_EXECUTOR_MODEL",
        "self_heal": "ANDREA_SELF_HEAL_CURSOR_EXECUTOR_MODEL",
        "telegram": "ANDREA_TELEGRAM_CURSOR_EXECUTOR_MODEL",
    }
    v = os.environ.get(overrides[lane], "").strip()
    if v:
        return v
    return (os.environ.get("ANDREA_CURSOR_EXECUTOR_MODEL") or "default").strip() or "default"


def _clip(text: str, limit: int = 12000) -> str:
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 3)].rstrip() + "..."


def run_cursor_handoff_cli(
    *,
    repo_path: Path,
    prompt: str,
    branch: str,
    cursor_mode: str,
    read_only: bool,
    model: str,
    poll_max_attempts: int,
    poll_interval_seconds: float,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Run cursor_handoff.py and return parsed JSON stdout plus subprocess metadata."""
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_HANDOFF_SCRIPT),
        "--repo",
        str(repo_path),
        "--prompt",
        prompt,
        "--mode",
        cursor_mode,
        "--read-only",
        "true" if read_only else "false",
        "--model",
        model,
        "--json",
        "--poll-max-attempts",
        str(poll_max_attempts),
        "--poll-interval-seconds",
        str(poll_interval_seconds),
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
        timeout=max(5, int(timeout_seconds)),
    )
    stdout = (proc.stdout or "").strip()
    payload: Dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
    ok = proc.returncode == 0 and bool(payload.get("ok"))
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": proc.stderr or "",
        "payload": payload,
        "command": cmd,
    }


def _collect_assistant_message_texts(node: Any, out: List[str]) -> None:
    if isinstance(node, dict):
        if str(node.get("type") or "") == "assistant_message":
            txt = str(node.get("text") or "").strip()
            if txt:
                out.append(txt)
        for v in node.values():
            _collect_assistant_message_texts(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_assistant_message_texts(item, out)


def fetch_agent_conversation_payload(*, repo_path: Path, agent_id: str, timeout_seconds: int = 90) -> Dict[str, Any]:
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_OPENCLAW_SCRIPT),
        "--json",
        "conversation",
        "--id",
        str(agent_id).strip(),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
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
    }


def submit_agent_followup_payload(
    *,
    repo_path: Path,
    agent_id: str,
    prompt: str,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    """POST follow-up prompt to an existing Cursor Cloud agent via cursor_openclaw.py."""
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_OPENCLAW_SCRIPT),
        "--json",
        "followup",
        "--id",
        str(agent_id).strip(),
        "--prompt",
        str(prompt or ""),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
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
    }


def fetch_agent_artifacts_payload(
    *, repo_path: Path, agent_id: str, timeout_seconds: int = 90
) -> Dict[str, Any]:
    """GET agent artifacts listing via cursor_openclaw.py."""
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_OPENCLAW_SCRIPT),
        "--json",
        "artifacts",
        "--id",
        str(agent_id).strip(),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
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
    }


def summarize_agent_terminal_state_from_response(response: Any) -> Dict[str, Any]:
    """Normalize agent-status / poll response into urls + status for continuity summaries."""
    if not isinstance(response, dict):
        return {"status": "", "agent_url": "", "pr_url": ""}
    st = str(response.get("status") or "").strip()
    target = response.get("target") if isinstance(response.get("target"), dict) else {}
    return {
        "status": st,
        "terminal_status": st.upper(),
        "agent_url": str(target.get("url") or "").strip(),
        "pr_url": str(target.get("prUrl") or "").strip(),
    }


def fetch_agent_status_payload(*, repo_path: Path, agent_id: str, timeout_seconds: int = 90) -> Dict[str, Any]:
    """Run cursor_openclaw.py agent-status and return parsed JSON."""
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_OPENCLAW_SCRIPT),
        "--json",
        "agent-status",
        "--id",
        str(agent_id).strip(),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
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
    }


def poll_cursor_agent_until_terminal(
    *,
    repo_path: Path,
    agent_id: str,
    max_attempts: int,
    interval_seconds: float,
    status_timeout_seconds: int = 60,
) -> Tuple[str, Dict[str, Any]]:
    """
    Poll Cloud Agents status until terminal or attempts exhausted.
    Returns (latest_status_upper, last_response_dict).
    """
    aid = str(agent_id or "").strip()
    latest = ""
    last_resp: Dict[str, Any] = {}
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        st = fetch_agent_status_payload(
            repo_path=repo_path,
            agent_id=aid,
            timeout_seconds=status_timeout_seconds,
        )
        resp = st.get("response") if isinstance(st.get("response"), dict) else {}
        last_resp = resp if isinstance(resp, dict) else {}
        raw = str(last_resp.get("status") or "").strip()
        latest = raw.upper()
        if latest in TERMINAL_CURSOR_AGENT_STATUSES:
            break
        if attempt < attempts - 1 and interval_seconds > 0:
            time.sleep(float(interval_seconds))
    return latest, last_resp


def enrich_handoff_payload_from_agent_status(payload: Dict[str, Any], last_resp: Dict[str, Any]) -> None:
    """Fill agent_url / pr_url from the last agent-status response when still empty."""
    target = last_resp.get("target") if isinstance(last_resp.get("target"), dict) else {}
    url = str(target.get("url") or "").strip()
    pr = str(target.get("prUrl") or "").strip()
    if url and not str(payload.get("agent_url") or "").strip():
        payload["agent_url"] = url
    if pr and not str(payload.get("pr_url") or "").strip():
        payload["pr_url"] = pr


def repair_handoff_poll_params() -> tuple[int, float]:
    """Poll settings for repair Cursor handoffs (ANDREA_REPAIR_CURSOR_*)."""
    max_raw = (os.environ.get("ANDREA_REPAIR_CURSOR_POLL_MAX_ATTEMPTS") or "3").strip()
    try:
        poll_max = max(0, min(120, int(max_raw)))
    except ValueError:
        poll_max = 3
    iv_raw = (os.environ.get("ANDREA_REPAIR_CURSOR_POLL_INTERVAL_SECONDS") or "3").strip()
    try:
        poll_iv = max(0.0, float(iv_raw))
    except ValueError:
        poll_iv = 3.0
    return poll_max, poll_iv


def repair_handoff_status_timeout_seconds() -> int:
    """Bounded timeout for each agent-status subprocess during repair polling."""
    raw = (os.environ.get("ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS") or "900").strip()
    try:
        t = max(30, int(raw))
    except ValueError:
        t = 900
    return min(120, t)


def extract_plan_text_from_conversation(response: Any) -> str:
    """
    Prefer markdown under ## CursorExecutionPlan; else use the last assistant_message text.
    """
    texts: List[str] = []
    _collect_assistant_message_texts(response, texts)
    if not texts:
        return ""
    combined = "\n\n".join(texts[-4:])
    marker = "## CursorExecutionPlan"
    if marker in combined:
        idx = combined.find(marker)
        return _clip(combined[idx:], 12000)
    return _clip(texts[-1], 12000)


def plan_text_usable(plan_text: str, *, min_chars: int = 80) -> bool:
    t = str(plan_text or "").strip()
    if len(t) < min_chars:
        return False
    lowered = t.lower()
    if "cursorexecutionplan" in lowered.replace(" ", ""):
        return True
    # Heuristic: numbered steps or file paths
    if "\n1." in t or "\n- " in t or "/" in t:
        return True
    return len(t) >= 200


def build_telegram_cursor_planner_prompt(user_task: str) -> str:
    """Read-only planner prompt for direct Telegram → Cursor tasks."""
    return (
        "You are a read-only planning agent for a subsequent Cursor Cloud execution pass.\n"
        "Do NOT edit files, create branches, or run mutating commands. Only produce a plan.\n\n"
        "User request:\n"
        f"{str(user_task or '').strip()}\n\n"
        "Respond with a markdown section exactly titled:\n"
        "## CursorExecutionPlan\n\n"
        "Include: files likely to touch, ordered steps, risks, and how to verify success.\n"
        "Keep the plan concise so an executor agent can follow it.\n"
    )


def build_self_heal_cursor_planner_prompt(proposal_body: str) -> str:
    """Read-only planner for auto-heal / optimizer proposals."""
    return (
        "You are a read-only planning agent for a subsequent Cursor execution pass.\n"
        "Do NOT modify the repository. Only output an execution plan.\n\n"
        f"{proposal_body.strip()}\n\n"
        "Respond with a markdown section exactly titled:\n"
        "## CursorExecutionPlan\n\n"
        "Include: files to touch, ordered steps, risks, and verification steps.\n"
    )


def self_heal_handoff_poll_params() -> tuple[int, float]:
    """Poll settings for self-heal handoffs; falls back to repair env vars."""
    max_raw = (
        os.environ.get("ANDREA_SELF_HEAL_CURSOR_POLL_MAX_ATTEMPTS", "").strip()
        or os.environ.get("ANDREA_REPAIR_CURSOR_POLL_MAX_ATTEMPTS", "").strip()
        or "3"
    )
    try:
        poll_max = max(0, min(120, int(max_raw)))
    except ValueError:
        poll_max = 3
    iv_raw = (
        os.environ.get("ANDREA_SELF_HEAL_CURSOR_POLL_INTERVAL_SECONDS", "").strip()
        or os.environ.get("ANDREA_REPAIR_CURSOR_POLL_INTERVAL_SECONDS", "").strip()
        or "3"
    )
    try:
        poll_iv = max(0.0, float(iv_raw))
    except ValueError:
        poll_iv = 3.0
    return poll_max, poll_iv


def self_heal_handoff_timeout_seconds() -> int:
    default_timeout = 900
    raw = (
        os.environ.get("ANDREA_SELF_HEAL_CURSOR_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS", "").strip()
    )
    if not raw:
        return default_timeout
    try:
        return max(30, int(raw))
    except ValueError:
        return default_timeout
