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
) -> str:
    collaboration_notes = ""
    collab = str(collaboration_mode or "auto").strip().lower() or "auto"
    if collab == "cursor_primary":
        collaboration_notes = (
            "- The user explicitly asked for Cursor. You should coordinate the handoff, but you must involve "
            "Cursor before giving the final answer unless the request is only a routing clarification.\n"
            "- After Cursor is involved, synthesize the outcome back into a concise assistant answer.\n"
        )
    elif collab == "collaborative":
        collaboration_notes = (
            "- The user wants Andrea/OpenClaw and Cursor to work together. Start with your own reasoning or skills, "
            "then involve Cursor for a second pass, heavier repo work, browsing/tool use, or implementation.\n"
            "- Your final answer should reflect the combined result, not just one side.\n"
        )
    return (
        f"You are running inside Andrea's lockstep OpenClaw execution lane for task {task_id}.\n\n"
        f"User request:\n{user_prompt.strip()}\n\n"
        "Execution rules:\n"
        "- Use OpenClaw skills first when they are the right fit.\n"
        "- If the task is repo-heavy, coding-heavy, debugging-heavy, or PR-oriented, use the cursor_handoff skill rather than answering from general model reasoning alone.\n"
        "- If you offload work to Cursor, wait for the useful outcome and summarize it clearly.\n"
        "- Keep the user-facing answer concise and directly useful.\n"
        f"{collaboration_notes}"
        "- End your response with exactly one single-line marker in this format:\n"
        '  LOCKSTEP_JSON: {"delegated_to_cursor":false,"cursor_agent_url":"","cursor_agent_id":"","pr_url":"","summary":"","status":"completed"}\n'
        "- Fill unknown string fields with an empty string.\n"
        "- Set delegated_to_cursor=true only if you actually used Cursor or cursor_handoff.\n"
        "- The summary field should be 1-2 sentences and must describe the final outcome.\n"
        "- Do not wrap the marker in a code block.\n\n"
        f"Local repository path for coding work: {repo_path}\n"
        f"Route reason: {route_reason or 'unspecified'}\n"
        f"Collaboration mode: {collab}\n"
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
    timeout_seconds: int,
    thinking: str,
) -> dict[str, Any]:
    message = _build_prompt(task_id, prompt, repo_path, route_reason, collaboration_mode)
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
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--agent-id", default="main")
    ap.add_argument("--route-reason", default="")
    ap.add_argument("--collaboration-mode", default="auto")
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
