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


def _footer_lines(
    task_id: str,
    status: str,
    *,
    agent_url: str = "",
    pr_url: str = "",
    last_error: str = "",
) -> list[str]:
    lines = [
        "Technical details:",
        f"- Task: {task_id}",
        f"- Status: {status}",
    ]
    if pr_url:
        lines.append(f"- PR: {pr_url}")
    if agent_url:
        lines.append(f"- Agent: {agent_url}")
    if last_error:
        lines.append(f"- Error: {_clip(last_error, 500)}")
    return lines


def format_ack_message(task_id: str) -> str:
    return "\n".join(
        [
            "Andrea:",
            "I got your message and queued it for Cursor.",
            "",
            "What happened:",
            "- Andrea created a task and will keep this thread updated.",
            "- Cursor will be started automatically.",
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


def format_running_message(task_id: str, agent_url: str = "") -> str:
    lines = [
        "Andrea:",
        "Cursor is actively working on your request now.",
        "",
        "What happened:",
        "- The task moved from queued to running.",
        "- I will send the result back here when it finishes.",
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
) -> str:
    summary_excerpt = _cursor_excerpt(summary)
    summary_sentence = _first_sentence(summary_excerpt)
    completed = status == "completed"
    if completed:
        if pr_url:
            andrea_line = "I finished your request and there is a PR ready to review."
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
        "- Cursor finished processing this task."
        if completed
        else "- Cursor ended in a failed state for this task.",
    ]
    if completed and pr_url:
        lines.append("- A PR is available for review.")
    elif completed and summary_sentence:
        lines.append(f"- Outcome: {summary_sentence}")
    elif not completed and last_error:
        lines.append(f"- Failure: {_clip(last_error, 220)}")

    if summary_excerpt:
        lines.extend(
            [
                "",
                "Cursor said:",
                summary_excerpt,
            ]
        )

    lines.extend(["", *_footer_lines(task_id, status, agent_url=agent_url, pr_url=pr_url, last_error=last_error)])
    return "\n".join(lines)
