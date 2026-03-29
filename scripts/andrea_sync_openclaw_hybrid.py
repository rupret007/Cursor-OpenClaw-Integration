#!/usr/bin/env python3
"""Run one OpenClaw hybrid task and normalize the result for andrea_sync."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.user_surface import (  # noqa: E402
    clip_text as shared_clip_text,
    dedupe_user_surface_items,
    is_internal_runtime_text as shared_is_internal_runtime_text,
    normalize_whitespace as shared_normalize_whitespace,
    sanitize_user_surface_text as shared_sanitize_user_surface_text,
    surface_similarity_key,
)

LOCKSTEP_JSON_PREFIX = "LOCKSTEP_JSON:"
CURSOR_AGENT_URL_RE = re.compile(r"https://cursor\.com/agents/([A-Za-z0-9._:-]+)")
PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")
ATTACHMENT_LIMIT_RE = re.compile(
    r"\b("
    r"attachments\.enabled|attachment[s]? (?:is |are )?(?:currently )?disabled|"
    r"cannot pass (?:detailed )?documents|can't pass (?:detailed )?documents|"
    r"blocked .*attachment"
    r")\b",
    re.I,
)
SESSION_ROUTING_RE = re.compile(
    r"\b("
    r"sessionkey|session key|session label|label that identifies|"
    r"unique session|identify(?:ing)? cursor'?s session|runtime identifiers?"
    r")\b",
    re.I,
)
def _clip(value: Any, limit: int) -> str:
    return shared_clip_text(value, limit)


def _normalize_whitespace(text: str) -> str:
    return shared_normalize_whitespace(text)


def _is_internal_runtime_text(text: str) -> bool:
    return shared_is_internal_runtime_text(text)


def _sanitize_user_safe_text(text: Any, limit: int = 500) -> str:
    return shared_sanitize_user_surface_text(text, limit=limit)


def _first_sentence(text: Any, limit: int = 320) -> str:
    clean = _normalize_whitespace(text)
    if not clean:
        return ""
    match = re.search(r"(?<=[.!?])\s+", clean)
    if match:
        clean = clean[: match.start()].strip()
    return _clip(clean, limit)


def _normalize_trace_item(value: Any) -> str:
    text = ""
    if isinstance(value, dict):
        lane = _normalize_whitespace(
            str(value.get("lane") or value.get("role") or value.get("worker") or "")
        )
        detail = _sanitize_user_safe_text(
            value.get("text")
            or value.get("summary")
            or value.get("step")
            or value.get("message")
            or "",
            240,
        )
        if lane and detail and not detail.lower().startswith(lane.lower()):
            text = f"{lane}: {detail}"
        else:
            text = detail or lane
    else:
        text = _sanitize_user_safe_text(value, 240)
    return _clip(text, 240)


def _coerce_collaboration_trace(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return dedupe_user_surface_items(
        (_normalize_trace_item(raw) for raw in value),
        limit=4,
        item_limit=240,
    )


def _derive_collaboration_trace(lockstep_meta: dict[str, Any], clean_text: str) -> list[str]:
    explicit = _coerce_collaboration_trace(lockstep_meta.get("collaboration_trace"))
    if explicit:
        return explicit
    items = dedupe_user_surface_items(str(clean_text or "").splitlines(), limit=4, item_limit=240)
    if items:
        return items
    safe_text = _sanitize_user_safe_text(clean_text, 800)
    if not safe_text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", safe_text)
    return dedupe_user_surface_items(sentences, limit=4, item_limit=240)


def _derive_blocked_reason(lockstep_meta: dict[str, Any], clean_text: str) -> str:
    explicit = _sanitize_user_safe_text(lockstep_meta.get("blocked_reason") or "", 280)
    if explicit:
        return explicit
    normalized = _normalize_whitespace(clean_text)
    if not normalized:
        return ""
    if ATTACHMENT_LIMIT_RE.search(normalized):
        return (
            "I hit an internal collaboration limitation while trying to pass work between reasoning "
            "lanes, so I could not complete that cross-model handoff cleanly."
        )
    if SESSION_ROUTING_RE.search(normalized):
        return (
            "OpenClaw handles session routing internally; you should not need session keys or "
            "runtime labels from me."
        )
    if _is_internal_runtime_text(normalized):
        return (
            "I ran into an internal collaboration limitation during the handoff, so I could not "
            "complete that cross-lane step cleanly."
        )
    return ""


def _infer_phase_label(text: str, lane: str = "") -> str:
    combined = f"{lane} {text}".lower()
    if any(
        keyword in combined
        for keyword in ("critique", "review", "double-check", "challenge", "second pass")
    ):
        return "critique"
    if any(
        keyword in combined
        for keyword in ("execute", "execution", "implement", "patch", "fix", "cursor", "run tests")
    ):
        return "execution"
    if any(keyword in combined for keyword in ("plan", "triage", "approach", "decompose", "outline")):
        return "plan"
    return "synthesis"


def _append_machine_trace_item(out: list[dict[str, str]], entry: dict[str, str]) -> None:
    signature = "|".join(
        [
            str(entry.get("phase") or ""),
            str(entry.get("lane") or ""),
            str(entry.get("provider") or ""),
            str(entry.get("model") or ""),
            str(entry.get("summary") or ""),
        ]
    )
    if not signature.strip("|"):
        return
    for existing in out:
        existing_signature = "|".join(
            [
                str(existing.get("phase") or ""),
                str(existing.get("lane") or ""),
                str(existing.get("provider") or ""),
                str(existing.get("model") or ""),
                str(existing.get("summary") or ""),
            ]
        )
        if existing_signature == signature:
            return
    out.append(entry)


def _collect_machine_trace(
    value: Any,
    out: list[dict[str, str]],
    *,
    default_lane: str = "",
    default_provider: str = "",
    default_model: str = "",
) -> None:
    if len(out) >= 8:
        return
    if isinstance(value, dict):
        lane = _normalize_whitespace(
            str(value.get("lane") or value.get("role") or value.get("worker") or default_lane or "")
        )
        provider = _normalize_whitespace(
            str(value.get("provider") or value.get("vendor") or default_provider or "")
        )
        model = _normalize_whitespace(str(value.get("model") or default_model or ""))
        text_value = (
            value.get("summary")
            or value.get("message")
            or value.get("text")
            or value.get("content")
            or ""
        )
        summary = _sanitize_user_safe_text(text_value, 240)
        if summary:
            _append_machine_trace_item(
                out,
                {
                    "phase": _infer_phase_label(summary, lane),
                    "lane": lane or "openclaw",
                    "provider": provider,
                    "model": model,
                    "summary": summary,
                },
            )
        for inner in value.values():
            _collect_machine_trace(
                inner,
                out,
                default_lane=lane or default_lane,
                default_provider=provider or default_provider,
                default_model=model or default_model,
            )
        return
    if isinstance(value, list):
        for inner in value:
            _collect_machine_trace(
                inner,
                out,
                default_lane=default_lane,
                default_provider=default_provider,
                default_model=default_model,
            )


def _derive_machine_collaboration_trace(
    payloads: Any,
    lockstep_meta: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    _collect_machine_trace(
        payloads,
        out,
        default_lane="openclaw",
        default_provider=provider,
        default_model=model,
    )
    if not out:
        _collect_machine_trace(
            lockstep_meta.get("phase_trace") or lockstep_meta.get("machine_collaboration_trace"),
            out,
            default_lane="openclaw",
            default_provider=provider,
            default_model=model,
        )
    return out[:6]


def _phase_summary_from_machine_trace(
    machine_trace: list[dict[str, str]], phase: str
) -> tuple[str, str]:
    for item in machine_trace:
        if str(item.get("phase") or "").strip().lower() != phase:
            continue
        summary = _sanitize_user_safe_text(item.get("summary") or "", 320)
        lane = _normalize_whitespace(str(item.get("lane") or ""))
        if summary or lane:
            return summary, lane
    return "", ""


def _derive_phase_outputs(
    lockstep_meta: dict[str, Any],
    *,
    summary: str,
    collaboration_trace: list[str],
    machine_trace: list[dict[str, str]],
    collaboration_mode: str,
    delegated_to_cursor: bool,
) -> dict[str, dict[str, str]]:
    collab = str(collaboration_mode or "").strip().lower()
    phase_key_map = {
        "plan": ("planner_summary", "plan_summary"),
        "critique": ("critic_summary", "critique_summary"),
        "execution": ("execution_summary", "executor_summary"),
        "synthesis": ("synthesis_summary", "final_summary"),
    }
    out: dict[str, dict[str, str]] = {}
    for phase, keys in phase_key_map.items():
        phase_summary = ""
        phase_lane = ""
        for key in keys:
            phase_summary = _sanitize_user_safe_text(lockstep_meta.get(key) or "", 320)
            if phase_summary:
                break
        if not phase_summary:
            phase_summary, phase_lane = _phase_summary_from_machine_trace(machine_trace, phase)
        if not phase_summary and phase == "plan":
            phase_summary = collaboration_trace[0] if collaboration_trace else summary
        if not phase_summary and phase == "critique" and collab in {"cursor_primary", "collaborative"}:
            phase_summary = "OpenClaw ran a critique pass before handing off or synthesizing the result."
        if not phase_summary and phase == "execution" and delegated_to_cursor:
            phase_summary = (
                "Execution used an OpenClaw skill handoff (for example cursor_handoff) after coordination."
            )
        if not phase_summary and phase == "synthesis":
            phase_summary = summary
        if not phase_summary:
            continue
        if phase == "synthesis" and surface_similarity_key(phase_summary) == surface_similarity_key(summary):
            continue
        if not phase_lane:
            phase_lane = "openclaw"
        out[phase] = {
            "lane": phase_lane,
            "status": "completed",
            "summary": phase_summary,
        }
    return out


def _build_prompt(
    task_id: str,
    user_prompt: str,
    repo_path: str,
    route_reason: str,
    collaboration_mode: str,
    preferred_model_family: str = "",
    preferred_model_label: str = "",
) -> str:
    collaboration_notes = ""
    collab = str(collaboration_mode or "auto").strip().lower() or "auto"
    preferred_family = str(preferred_model_family or "").strip().lower()
    preferred_label = str(preferred_model_label or "").strip()
    if collab == "cursor_primary":
        collaboration_notes = (
            "- The user chose a deeper execution / @cursor-style ask. Stay entirely inside OpenClaw: use verified "
            "skills (including cursor_handoff when repo edits or PRs are truly needed).\n"
            "- Complete the work in this session when possible; synthesize a concise user-facing outcome.\n"
            "- Never ask the user for session keys, labels, runtime identifiers, or tool routing details.\n"
        )
    elif collab == "collaborative":
        collaboration_notes = (
            "- The user wants a visible multi-step collaboration. You are the conductor inside this OpenClaw run; "
            "Andrea already applied routing and channel policy.\n"
            "- Start with triage and the best OpenClaw skills for each subtask (calendar, messaging, notes, "
            "cursor_handoff for repo work, web/search when grounded, etc.).\n"
            "- Use model strengths when available inside OpenClaw:\n"
            "  - Gemini 2.5 for broad planning, decomposition, and first-pass reasoning.\n"
            "  - Minimax 2.7 for alternative critique, divergence, or second-opinion analysis.\n"
            "  - OpenAI for precise synthesis, instruction following, and tool-friendly substeps.\n"
            "- Do not call every model for every task. Use only what the subtask needs.\n"
            "- Stay in one coordinated OpenClaw session unless a supported child-session handoff clearly helps.\n"
            "- When code or config changes happen, include verification in the execution/synthesis story.\n"
            "- Your final answer should reflect the combined result. Keep collaboration traces sparse and user-safe.\n"
        )
    preferred_model_note = ""
    if preferred_family:
        preferred_model_note = (
            f"- The user explicitly addressed the {preferred_label or preferred_family.title()} lane.\n"
            f"- Start in that lane when it is available, unless reliability or tool constraints require a safer fallback.\n"
            "- If you do fall back, say so briefly in the collaboration transcript.\n"
        )
    return (
        f"You are running inside Andrea's lockstep OpenClaw execution lane for task {task_id}.\n\n"
        f"User request:\n{user_prompt.strip()}\n\n"
        "Execution rules:\n"
        "- Use OpenClaw skills first when they are the right fit (productivity, search, messaging, repo handoff, etc.).\n"
        "- If the task is repo-heavy, coding-heavy, debugging-heavy, or PR-oriented, prefer the cursor_handoff skill rather than guessing from base model text alone.\n"
        "- When a skill or tool run produces the outcome, wait for it to finish and summarize clearly for the user.\n"
        "- Prefer a disciplined flow: triage, plan, critique when useful, execute, verify, then synthesize.\n"
        "- Keep the user-facing answer concise and directly useful.\n"
        "- When collaboration is requested, think like a coordinator: triage first, assign the right model/tool to the right subtask, then synthesize the result.\n"
        "- Do not expose tool names, config flags, session identifiers, session labels, or runtime mechanics in user-facing text.\n"
        "- If a handoff is blocked, explain it in calm product language for the user and keep the exact runtime diagnostic in internal_trace only.\n"
        "- The JSON fields are authoritative. Any prose before the marker is optional and should stay user-safe.\n"
        f"{collaboration_notes}"
        f"{preferred_model_note}"
        "- End your response with exactly one single-line marker in this format:\n"
        '  LOCKSTEP_JSON: {"delegated_to_cursor":false,"cursor_agent_url":"","cursor_agent_id":"","pr_url":"","summary":"","status":"completed","planner_summary":"","critic_summary":"","execution_summary":"","synthesis_summary":"","phase_trace":[],"collaboration_trace":[],"blocked_reason":"","internal_trace":""}\n'
        "- Fill unknown string fields with an empty string.\n"
        "- Set delegated_to_cursor=true only if you actually invoked cursor_handoff or another execution that reports a Cursor agent URL/PR in the lockstep contract.\n"
        "- The summary field should be 1-2 sentences and must describe the final outcome.\n"
        "- planner_summary, critic_summary, execution_summary, and synthesis_summary should stay concise and user-safe.\n"
        "- phase_trace should be a short array of structured step objects when possible, e.g. {\"phase\":\"plan\",\"lane\":\"openclaw\",\"summary\":\"...\"}.\n"
        "- collaboration_trace should be a short array of 0-4 meaningful steps. Keep each step user-safe and concise.\n"
        "- blocked_reason should stay empty unless a real product-level limitation or fallback needs to be explained.\n"
        "- internal_trace should contain exact runtime/tool diagnostics only when needed for debugging or optimization.\n"
        "- Do not wrap the marker in a code block.\n\n"
        f"Local repository path for coding work: {repo_path}\n"
        f"Route reason: {route_reason or 'unspecified'}\n"
        f"Collaboration mode: {collab}\n"
        f"Preferred model family: {preferred_family or 'none'}\n"
    )


def _collect_text_parts(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("text"), str) and value.get("text", "").strip():
            out.append(str(value["text"]).strip())
        for inner in value.values():
            _collect_text_parts(inner, out)
        return
    if isinstance(value, list):
        for inner in value:
            _collect_text_parts(inner, out)


def _extract_lockstep_json(text: str) -> tuple[str, dict[str, Any]]:
    lines = str(text or "").splitlines()
    meta: dict[str, Any] = {}
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(LOCKSTEP_JSON_PREFIX):
            raw = stripped[len(LOCKSTEP_JSON_PREFIX) :].strip()
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    meta = decoded
                    continue
            except json.JSONDecodeError:
                pass
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned, meta


def _extract_urls(text: str) -> tuple[str, str, str]:
    text = str(text or "")
    agent_match = CURSOR_AGENT_URL_RE.search(text)
    agent_url = agent_match.group(0) if agent_match else ""
    agent_id = agent_match.group(1) if agent_match else ""
    pr_match = PR_URL_RE.search(text)
    pr_url = pr_match.group(0) if pr_match else ""
    return agent_url, agent_id, pr_url


def _derive_openclaw_contract(
    lockstep_meta: dict[str, Any],
    clean_text: str,
    payload: dict[str, Any],
    *,
    payloads: Any = None,
    collaboration_mode: str = "",
    delegated_to_cursor: bool = False,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    summary = _sanitize_user_safe_text(lockstep_meta.get("summary") or "", 2000)
    if not summary:
        summary = _sanitize_user_safe_text(payload.get("summary") or "", 2000)
    for key in ("message", "response", "answer", "output"):
        if summary:
            break
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            summary = _sanitize_user_safe_text(val, 2000)
        elif isinstance(val, dict):
            for sub in ("text", "message", "content"):
                inner = val.get(sub)
                if isinstance(inner, str) and inner.strip():
                    summary = _sanitize_user_safe_text(inner, 2000)
                    break
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for key in ("summary", "message", "text"):
        if summary:
            break
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            summary = _sanitize_user_safe_text(val, 2000)
    blocked_reason = _derive_blocked_reason(lockstep_meta, clean_text)
    if not summary and blocked_reason:
        summary = blocked_reason
    if not summary:
        summary = _sanitize_user_safe_text(clean_text, 2000)
    collaboration_trace = _derive_collaboration_trace(lockstep_meta, clean_text)
    collaboration_trace = dedupe_user_surface_items(
        collaboration_trace,
        suppress_against=[summary],
        limit=4,
        item_limit=240,
    )
    machine_trace = _derive_machine_collaboration_trace(
        payloads,
        lockstep_meta,
        provider=provider,
        model=model,
    )
    return {
        "summary": summary,
        "collaboration_trace": collaboration_trace,
        "machine_collaboration_trace": machine_trace,
        "phase_outputs": _derive_phase_outputs(
            lockstep_meta,
            summary=summary,
            collaboration_trace=collaboration_trace,
            machine_trace=machine_trace,
            collaboration_mode=collaboration_mode,
            delegated_to_cursor=delegated_to_cursor,
        ),
        "blocked_reason": blocked_reason,
        "internal_trace": _clip(
            str(lockstep_meta.get("internal_trace") or clean_text or payload.get("error") or ""),
            4000,
        ),
    }


def run_openclaw_hybrid(
    *,
    task_id: str,
    prompt: str,
    repo_path: str,
    agent_id: str,
    session_id: str,
    route_reason: str,
    collaboration_mode: str,
    preferred_model_family: str,
    preferred_model_label: str,
    timeout_seconds: int,
    thinking: str,
) -> dict[str, Any]:
    message = _build_prompt(
        task_id,
        prompt,
        repo_path,
        route_reason,
        collaboration_mode,
        preferred_model_family,
        preferred_model_label,
    )
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        agent_id,
        "--message",
        message,
        "--json",
        "--timeout",
        str(max(1, timeout_seconds)),
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if thinking:
        cmd.extend(["--thinking", thinking])
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=max(5, timeout_seconds + 5),
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if not stdout:
        raise RuntimeError(f"empty OpenClaw response: {stderr[:500]}")
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid OpenClaw JSON: {stdout[:500]}") from exc
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"empty OpenClaw JSON payload: {stderr[:500] or stdout[:500]}")
    if proc.returncode != 0:
        detail = ""
        if isinstance(payload, dict):
            detail = _normalize_whitespace(
                payload.get("error") or payload.get("summary") or payload.get("status") or ""
            )
        raise RuntimeError(detail or stderr or stdout[:500] or f"openclaw exit={proc.returncode}")

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    payloads = result.get("payloads") if isinstance(result.get("payloads"), list) else []
    texts: list[str] = []
    _collect_text_parts(payloads, texts)
    combined_text = "\n".join(texts).strip()
    clean_text, lockstep_meta = _extract_lockstep_json(combined_text)
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    system_prompt_report = (
        meta.get("systemPromptReport") if isinstance(meta.get("systemPromptReport"), dict) else {}
    )
    provider_text = str(
        agent_meta.get("provider") or system_prompt_report.get("provider") or ""
    ).strip()
    model_text = str(agent_meta.get("model") or system_prompt_report.get("model") or "").strip()

    agent_url, cursor_agent_id, pr_url = _extract_urls(clean_text)
    if isinstance(lockstep_meta, dict):
        agent_url = str(lockstep_meta.get("cursor_agent_url") or agent_url).strip()
        cursor_agent_id = str(lockstep_meta.get("cursor_agent_id") or cursor_agent_id).strip()
        pr_url = str(lockstep_meta.get("pr_url") or pr_url).strip()

    delegated_to_cursor = bool(lockstep_meta.get("delegated_to_cursor", False))
    delegated_to_cursor = delegated_to_cursor or bool(agent_url or pr_url or cursor_agent_id)

    contract = _derive_openclaw_contract(
        lockstep_meta,
        clean_text,
        payload,
        payloads=payloads,
        collaboration_mode=collaboration_mode,
        delegated_to_cursor=delegated_to_cursor,
        provider=provider_text,
        model=model_text,
    )
    summary = str(contract.get("summary") or "").strip()
    if not summary:
        summary = "OpenClaw completed the delegated task."
    user_summary = _first_sentence(summary, 320) or _clip(summary, 500)
    status = str(lockstep_meta.get("status") or payload.get("status") or "ok").strip().lower()
    ok = status in {"ok", "completed", "success"}

    return {
        "ok": ok,
        "backend": "openclaw",
        "execution_lane": "openclaw_hybrid",
        "delegated_to_cursor": delegated_to_cursor,
        "summary": _clip(summary, 2000),
        "user_summary": user_summary,
        "collaboration_trace": contract.get("collaboration_trace") or [],
        "machine_collaboration_trace": contract.get("machine_collaboration_trace") or [],
        "phase_outputs": contract.get("phase_outputs") or {},
        "blocked_reason": _clip(contract.get("blocked_reason"), 500),
        "internal_trace": _clip(contract.get("internal_trace"), 4000),
        "raw_text": _clip(clean_text, 4000),
        "openclaw_run_id": str(payload.get("runId") or "").strip(),
        "openclaw_session_id": str(agent_meta.get("sessionId") or system_prompt_report.get("sessionId") or "").strip(),
        "provider": provider_text,
        "model": model_text,
        "cursor_agent_id": cursor_agent_id,
        "agent_url": agent_url,
        "pr_url": pr_url,
        "status": status,
        "stop_reason": str(result.get("stopReason") or "").strip(),
        "collaboration_mode": collaboration_mode,
        "preferred_model_family": preferred_model_family,
        "preferred_model_label": preferred_model_label,
        "requested_session_id": session_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--agent-id", default="main")
    ap.add_argument("--session-id", default="")
    ap.add_argument("--route-reason", default="")
    ap.add_argument("--collaboration-mode", default="auto")
    ap.add_argument("--preferred-model-family", default="")
    ap.add_argument("--preferred-model-label", default="")
    ap.add_argument("--timeout-seconds", type=int, default=900)
    ap.add_argument("--thinking", default="medium")
    args = ap.parse_args()
    try:
        result = run_openclaw_hybrid(
            task_id=args.task_id,
            prompt=args.prompt,
            repo_path=args.repo,
            agent_id=args.agent_id,
            session_id=str(args.session_id or "").strip(),
            route_reason=args.route_reason,
            collaboration_mode=str(args.collaboration_mode or "").strip() or "auto",
            preferred_model_family=str(args.preferred_model_family or "").strip(),
            preferred_model_label=str(args.preferred_model_label or "").strip(),
            timeout_seconds=max(1, args.timeout_seconds),
            thinking=str(args.thinking or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": _clip(exc, 2000)}))
        return 1
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
