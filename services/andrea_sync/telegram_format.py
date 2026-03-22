"""Format Telegram-facing Andrea replies from projected task state."""
from __future__ import annotations

import re
from typing import Any, Dict


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    cut = max(0, limit - 3)
    return text[:cut].rstrip() + "..."


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _cursor_excerpt(summary: str, limit: int = 700) -> str:
    lines: list[str] = []
    for raw_line in str(summary or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line.startswith("* "):
            line = line[2:].strip()
        lines.append(line)
    joined = _normalize_whitespace(" ".join(lines))
    return _clip(joined, limit)


def _first_sentence(text: str, limit: int = 180) -> str:
    clean = _normalize_whitespace(text)
    if not clean:
        return ""
    match = re.search(r"(?<=[.!?])\s+", clean)
    if match:
        clean = clean[: match.start()].strip()
    return _clip(clean, limit)


def _routing_note(routing_hint: str, collaboration_mode: str) -> str:
    hint = str(routing_hint or "").strip().lower()
    collab = str(collaboration_mode or "").strip().lower()
    if hint == "andrea":
        return "- You addressed Andrea directly, so I kept this in the assistant lane."
    if hint == "cursor":
        return "- You addressed Cursor directly, so Andrea routed this as a Cursor-first collaboration."
    if hint == "collaborate" or collab == "collaborative":
        return "- You asked Andrea and Cursor to work together on this."
    return ""


def _footer_lines(
    task_id: str,
    status: str,
    *,
    agent_url: str = "",
    pr_url: str = "",
    last_error: str = "",
    openclaw_session_id: str = "",
) -> list[str]:
    lines = [
        "Technical details:",
        f"- Task: {task_id}",
        f"- Status: {status}",
    ]
    if openclaw_session_id:
        lines.append(f"- OpenClaw session: {_clip(openclaw_session_id, 200)}")
    if pr_url:
        lines.append(f"- PR: {pr_url}")
    if agent_url:
        lines.append(f"- Agent: {agent_url}")
    if last_error:
        lines.append(f"- Error: {_clip(last_error, 500)}")
    return lines


def format_ack_message(
    task_id: str,
    *,
    worker_label: str = "Cursor",
    routing_hint: str = "",
    collaboration_mode: str = "",
) -> str:
    routing_note = _routing_note(routing_hint, collaboration_mode)
    if worker_label == "OpenClaw":
        body = [
            "Andrea:",
            "I got your message and queued it for OpenClaw.",
            "",
            "What happened:",
            "- Andrea created a task and will keep this thread updated.",
            "- OpenClaw will handle this directly or bring in Cursor if the work needs deeper repo execution.",
            *([routing_note] if routing_note else []),
            "",
            "Technical details:",
            f"- Task: {task_id}",
            "- Status: queued",
        ]
        return "\n".join(body)
    return "\n".join(
        [
            "Andrea:",
            "I got your message and queued it for Cursor.",
            "",
            "What happened:",
            "- Andrea created a task and will keep this thread updated.",
            "- Cursor will be started automatically.",
            *([routing_note] if routing_note else []),
            "",
            "Technical details:",
            f"- Task: {task_id}",
            "- Status: queued",
        ]
    )


def format_direct_message(reply_text: str) -> str:
    clean = _normalize_whitespace(reply_text)
    return "\n".join(
        [
            "Andrea:",
            _clip(clean, 500) or "I'm here and ready to help.",
        ]
    )


def format_running_message(
    task_id: str,
    agent_url: str = "",
    *,
    worker_label: str = "Cursor",
    delegated_to_cursor: bool = False,
    routing_hint: str = "",
    collaboration_mode: str = "",
) -> str:
    if delegated_to_cursor and worker_label == "OpenClaw":
        worker_label = "OpenClaw and Cursor"
    if worker_label == "OpenClaw and Cursor":
        headline = "OpenClaw and Cursor are actively working on your request now."
        bullets = [
            "- OpenClaw is coordinating the task and Cursor has been pulled in for the heavier execution.",
            "- I will send the result back here when it finishes.",
        ]
    elif worker_label == "OpenClaw":
        headline = "OpenClaw is actively working on your request now."
        bullets = [
            "- The task moved from queued to running inside the OpenClaw lane.",
            "- I will send the result back here when it finishes.",
        ]
    else:
        headline = "Cursor is actively working on your request now."
        bullets = [
            "- The task moved from queued to running.",
            "- I will send the result back here when it finishes.",
        ]
    lines = [
        "Andrea:",
        headline,
        "",
        "What happened:",
        *bullets,
        *([_routing_note(routing_hint, collaboration_mode)] if _routing_note(routing_hint, collaboration_mode) else []),
        "",
    ]
    lines.extend(_footer_lines(task_id, "running", agent_url=agent_url))
    return "\n".join(lines)


def format_final_message(
    task_id: str,
    *,
    status: str,
    summary: str = "",
    pr_url: str = "",
    agent_url: str = "",
    last_error: str = "",
    worker_label: str = "Cursor",
    delegated_to_cursor: bool = False,
    backend: str = "",
    openclaw_session_id: str = "",
    routing_hint: str = "",
    collaboration_mode: str = "",
) -> str:
    if backend == "openclaw" and worker_label == "Cursor":
        worker_label = "OpenClaw"
    summary_excerpt = _cursor_excerpt(summary)
    summary_sentence = _first_sentence(summary_excerpt)
    completed = status == "completed"
    result_label = "Cursor"
    if worker_label == "OpenClaw":
        result_label = "OpenClaw"
    elif worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
        result_label = "Cursor"
    if completed:
        if pr_url:
            if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
                andrea_line = "I finished your request and there is a PR ready to review."
            else:
                andrea_line = "I finished your request and OpenClaw completed it successfully."
        elif summary_sentence:
            andrea_line = f"I finished your request. {summary_sentence}"
        else:
            andrea_line = "I finished your request and captured the result below."
    else:
        andrea_line = "I could not complete your request successfully, but I captured the failure details below."

    lines = [
        "Andrea:",
        andrea_line,
        "",
        "What happened:",
        (
            "- OpenClaw coordinated this task and Cursor finished the heavy execution."
            if completed and (worker_label == "OpenClaw and Cursor" or delegated_to_cursor)
            else "- OpenClaw finished processing this task."
            if completed and worker_label == "OpenClaw"
            else "- Cursor finished processing this task."
            if completed
            else "- OpenClaw ended in a failed state for this task."
            if worker_label == "OpenClaw"
            else "- Cursor ended in a failed state for this task."
        ),
    ]
    if completed and pr_url:
        lines.append("- A PR is available for review.")
    elif completed and summary_sentence:
        lines.append(f"- Outcome: {summary_sentence}")
    elif not completed and last_error:
        lines.append(f"- Failure: {_clip(last_error, 220)}")
    routing_note = _routing_note(routing_hint, collaboration_mode)
    if routing_note:
        lines.append(routing_note)

    if summary_excerpt:
        lines.extend(
            [
                "",
                f"{result_label} said:",
                summary_excerpt,
            ]
        )

    lines.extend(
        [
            "",
            *_footer_lines(
                task_id,
                status,
                agent_url=agent_url,
                pr_url=pr_url,
                last_error=last_error,
                openclaw_session_id=openclaw_session_id,
            ),
        ]
    )
    return "\n".join(lines)
