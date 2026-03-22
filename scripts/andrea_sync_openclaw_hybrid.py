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
LOCKSTEP_JSON_PREFIX = "LOCKSTEP_JSON:"
CURSOR_AGENT_URL_RE = re.compile(r"https://cursor\.com/agents/([A-Za-z0-9._:-]+)")
PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _build_prompt(
    task_id: str,
    user_prompt: str,
    repo_path: str,
    route_reason: str,
    collaboration_mode: str,
    explicit_cursor_requested: bool,
    preferred_model_family: str = "",
    preferred_model_label: str = "",
) -> str:
    collaboration_notes = ""
    collab = str(collaboration_mode or "auto").strip().lower() or "auto"
    preferred_family = str(preferred_model_family or "").strip().lower()
    preferred_label = str(preferred_model_label or "").strip()
    if collab == "cursor_primary":
        collaboration_notes = (
            "- The user explicitly asked for Cursor. You should coordinate the handoff, but you must involve "
            "Cursor before giving the final answer unless the request is only a routing clarification.\n"
            "- After Cursor is involved, synthesize the outcome back into a concise assistant answer.\n"
            "- Use OpenClaw for triage and coordination first, then move the repo-heavy execution into Cursor.\n"
        )
    elif collab == "collaborative":
        collaboration_notes = (
            "- The user wants a visible collaboration sprint between Andrea/OpenClaw and Cursor.\n"
            "- Start with your own reasoning or skills, then involve Cursor for a second pass, heavier repo work, "
            "browsing/tool use, or implementation.\n"
            "- Use model strengths deliberately when they are available inside OpenClaw:\n"
            "  - Gemini 2.5 for broad planning, decomposition, and first-pass reasoning.\n"
            "  - Minimax 2.7 for alternative critique, divergence, or second-opinion analysis.\n"
            "  - OpenAI for precise synthesis, instruction following, and tool-friendly substeps.\n"
            "  - Cursor for the heavy repo execution, code edits, and implementation follow-through.\n"
            "- Do not call every model for every task. Use only the best model needed for each subtask.\n"
            "- Your final answer should reflect the combined result, not just one side.\n"
            "- Include a short collaboration transcript in natural language before the LOCKSTEP_JSON marker. "
            "Mention which model/provider or execution lane handled which part of the work.\n"
        )
    preferred_model_note = ""
    if preferred_family:
        preferred_model_note = (
            f"- The user explicitly addressed the {preferred_label or preferred_family.title()} lane.\n"
            f"- Start in that lane when it is available, unless reliability or tool constraints require a safer fallback.\n"
            "- If you do fall back, say so briefly in the collaboration transcript.\n"
        )
    cursor_safety_note = ""
    if not explicit_cursor_requested:
        cursor_safety_note = (
            "- IMPORTANT: Do not invoke Cursor or the cursor_handoff skill unless the user explicitly asked for Cursor.\n"
            "- If the work is repo-heavy and you would normally delegate to Cursor, instead provide a safe OpenClaw-only answer: "
            "clarify what you can do within OpenClaw, propose minimal-risk next steps, and ask for explicit Cursor permission if needed.\n"
        )
    return (
        f"You are running inside Andrea's lockstep OpenClaw execution lane for task {task_id}.\n\n"
        f"User request:\n{user_prompt.strip()}\n\n"
        "Execution rules:\n"
        "- Use OpenClaw skills first when they are the right fit.\n"
        f"{cursor_safety_note}"
        "- If the user explicitly asked for Cursor and the task is repo-heavy, coding-heavy, debugging-heavy, or PR-oriented, "
        "use the cursor_handoff skill rather than answering from general model reasoning alone.\n"
        "- If you do offload work to Cursor, wait for the useful outcome and summarize it clearly.\n"
        "- Keep the user-facing answer concise and directly useful.\n"
        "- When collaboration is requested, think like a coordinator: triage first, assign the right model/tool to the right subtask, then synthesize the result.\n"
        f"{collaboration_notes}"
        f"{preferred_model_note}"
        "- End your response with exactly one single-line marker in this format:\n"
        '  LOCKSTEP_JSON: {"delegated_to_cursor":false,"cursor_agent_url":"","cursor_agent_id":"","pr_url":"","summary":"","status":"completed"}\n'
        "- Fill unknown string fields with an empty string.\n"
        "- Set delegated_to_cursor=true only if you actually used Cursor or cursor_handoff.\n"
        "- The summary field should be 1-2 sentences and must describe the final outcome.\n"
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


def run_openclaw_hybrid(
    *,
    task_id: str,
    prompt: str,
    repo_path: str,
    agent_id: str,
    route_reason: str,
    collaboration_mode: str,
    preferred_model_family: str,
    preferred_model_label: str,
    timeout_seconds: int,
    thinking: str,
    explicit_cursor_requested: bool,
) -> dict[str, Any]:
    message = _build_prompt(
        task_id,
        prompt,
        repo_path,
        route_reason,
        collaboration_mode,
        explicit_cursor_requested,
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

    agent_url, cursor_agent_id, pr_url = _extract_urls(clean_text)
    if isinstance(lockstep_meta, dict):
        agent_url = str(lockstep_meta.get("cursor_agent_url") or agent_url).strip()
        cursor_agent_id = str(lockstep_meta.get("cursor_agent_id") or cursor_agent_id).strip()
        pr_url = str(lockstep_meta.get("pr_url") or pr_url).strip()

    delegated_to_cursor = bool(lockstep_meta.get("delegated_to_cursor", False))
    delegated_to_cursor = delegated_to_cursor or bool(agent_url or pr_url or cursor_agent_id)

    summary = _normalize_whitespace(
        str(lockstep_meta.get("summary") or clean_text or payload.get("summary") or "").strip()
    )
    if not summary:
        summary = "OpenClaw completed the delegated task."
    status = str(lockstep_meta.get("status") or payload.get("status") or "ok").strip().lower()
    ok = status in {"ok", "completed", "success"}

    return {
        "ok": ok,
        "backend": "openclaw",
        "execution_lane": "openclaw_hybrid",
        "delegated_to_cursor": delegated_to_cursor,
        "summary": _clip(summary, 2000),
        "raw_text": _clip(clean_text, 4000),
        "openclaw_run_id": str(payload.get("runId") or "").strip(),
        "openclaw_session_id": str(agent_meta.get("sessionId") or system_prompt_report.get("sessionId") or "").strip(),
        "provider": str(agent_meta.get("provider") or system_prompt_report.get("provider") or "").strip(),
        "model": str(agent_meta.get("model") or system_prompt_report.get("model") or "").strip(),
        "cursor_agent_id": cursor_agent_id,
        "agent_url": agent_url,
        "pr_url": pr_url,
        "status": status,
        "stop_reason": str(result.get("stopReason") or "").strip(),
        "collaboration_mode": collaboration_mode,
        "preferred_model_family": preferred_model_family,
        "preferred_model_label": preferred_model_label,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--agent-id", default="main")
    ap.add_argument("--route-reason", default="")
    ap.add_argument("--collaboration-mode", default="auto")
    ap.add_argument("--explicit-cursor-requested", default="0")
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
            route_reason=args.route_reason,
            collaboration_mode=str(args.collaboration_mode or "").strip() or "auto",
            explicit_cursor_requested=str(args.explicit_cursor_requested or "0").strip().lower()
            in {"1", "true", "yes", "on"},
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
