"""Format Telegram-facing Andrea replies from projected task state."""
from __future__ import annotations

import re
from typing import Any, Dict

from .user_surface import (
    dedupe_user_surface_items,
    normalize_whitespace as shared_normalize_whitespace,
    sanitize_user_surface_text,
    strip_conversational_soft_failure_boilerplate,
    surface_similarity_key,
)

SOFT_FAILURE_SUMMARY_RE = re.compile(
    r"\b("
    r"unable\s+to\s+hand\s+off|"
    r"could\s+not\s+hand\s+off|"
    r"failed\s+to\s+hand\s+off|"
    r"unable\s+to\s+delegate|"
    r"could\s+not\s+delegate|"
    r"could\s+not\s+complete|"
    r"unable\s+to\s+complete|"
    r"did\s+not\s+complete"
    r")\b",
    re.I,
)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    cut = max(0, limit - 3)
    return text[:cut].rstrip() + "..."


def _normalize_whitespace(text: str) -> str:
    return shared_normalize_whitespace(text)


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


def _model_label(provider: str = "", model: str = "") -> str:
    clean_provider = _normalize_whitespace(provider)
    clean_model = _normalize_whitespace(model)
    if clean_provider and clean_model:
        return f"{clean_provider} / {clean_model}"
    return clean_model or clean_provider


def _preferred_model_note(preferred_model_label: str = "") -> str:
    clean = _normalize_whitespace(preferred_model_label)
    if not clean:
        return ""
    return f"- Preferred OpenClaw lane: {clean}"


def _speaker_section_label(
    *,
    worker_label: str,
    delegated_to_cursor: bool = False,
    provider: str = "",
    model: str = "",
    preferred_model_label: str = "",
) -> str:
    model_label = _model_label(provider, model)
    preferred_label = _normalize_whitespace(preferred_model_label)
    if worker_label == "OpenClaw":
        if model_label:
            return f"OpenClaw coordinator ({model_label})"
        if preferred_label:
            return f"OpenClaw coordinator ({preferred_label} lane)"
        return "OpenClaw"
    if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
        if model_label:
            return f"OpenClaw coordinator ({model_label}) and Cursor"
        if preferred_label:
            return f"OpenClaw coordinator ({preferred_label} lane) and Cursor"
        return "OpenClaw and Cursor"
    return "Cursor"


def _routing_note(routing_hint: str, collaboration_mode: str) -> str:
    hint = str(routing_hint or "").strip().lower()
    collab = str(collaboration_mode or "").strip().lower()
    if hint == "andrea":
        return "- You addressed Andrea directly, so I kept this in the assistant lane."
    if hint == "cursor":
        return "- You asked Andrea to use the Cursor execution lane for the heavy work."
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
    if pr_url:
        lines.append(f"- PR: {pr_url}")
    if agent_url:
        lines.append(f"- Agent: {agent_url}")
    if last_error:
        lines.append(f"- Error: {_clip(last_error, 500)}")
    return lines


def _collaboration_trace_lines(
    collaboration_trace: list[str] | None,
    *,
    visibility_mode: str = "",
    suppress_against: list[str] | None = None,
) -> list[str]:
    if str(visibility_mode or "").strip().lower() != "full":
        return []
    if not isinstance(collaboration_trace, list):
        return []
    items = dedupe_user_surface_items(
        collaboration_trace,
        limit=4,
        item_limit=240,
        suppress_against=suppress_against or [],
    )
    rendered = [f"- {_clip(text, 240)}" for text in items]
    if not rendered:
        return []
    return ["", "Collaboration trace:", *rendered]


def format_ack_message(
    task_id: str,
    *,
    worker_label: str = "Cursor",
    auto_start: bool = True,
    routing_hint: str = "",
    collaboration_mode: str = "",
    preferred_model_label: str = "",
) -> str:
    routing_note = _routing_note(routing_hint, collaboration_mode)
    preferred_model_note = _preferred_model_note(preferred_model_label)
    if worker_label == "OpenClaw":
        execution_line = "- OpenClaw will be started automatically."
        if not auto_start:
            execution_line = "- OpenClaw is queued for manual start or a later retry."
        body = [
            "Andrea:",
            "OpenClaw is taking point — it coordinates first, then delegates to Cursor when the repo needs execution.",
            "",
            "What happens next:",
            "- OpenClaw runs the coordination / handoff pass (same flow as before).",
            execution_line,
            "- Status updates are threaded under your message so this chat stays readable.",
            *([preferred_model_note] if preferred_model_note else []),
            *([routing_note] if routing_note else []),
            "",
            "Technical details:",
            f"- Task: {task_id}",
            "- Status: queued",
        ]
        return "\n".join(body)
    execution_line = "- Cursor will be started automatically."
    if not auto_start:
        execution_line = "- Cursor is queued for execution, but auto-start is currently disabled."
    return "\n".join(
        [
            "Andrea:",
            "I got your message and queued it for Cursor.",
            "",
            "What happens next:",
            "- Andrea created a task and will keep this thread updated.",
            execution_line,
            *([preferred_model_note] if preferred_model_note else []),
            *([routing_note] if routing_note else []),
            "",
            "Technical details:",
            f"- Task: {task_id}",
            "- Status: queued",
        ]
    )


def format_continuation_notice(
    task_id: str,
    *,
    chunk_preview: str = "",
    worker_label: str = "OpenClaw",
    routing_hint: str = "",
    collaboration_mode: str = "",
) -> str:
    """Short Telegram copy when a follow-up message was merged onto the current task."""
    preview = _clip(chunk_preview, 100)
    lane_line = "OpenClaw keeps one coordination run"
    if worker_label == "OpenClaw and Cursor":
        lane_line = "OpenClaw and Cursor stay on one shared heavy-lift workstream"
    elif worker_label == "Cursor":
        lane_line = "Cursor stays on one execution run"
    routing_note = _routing_note(routing_hint, collaboration_mode)
    lines = [
        "Andrea:",
        f"I folded this into the current heavy-lift task `{task_id}` so Andrea and Cursor stay on one workstream (no duplicate job).",
        f"{lane_line}. I'll treat your latest message as the next instruction and keep the result in the same thread.",
    ]
    if preview:
        lines.append(f"Latest chunk: {preview}")
    if routing_note:
        lines.append(routing_note.strip())
    return "\n".join(lines)


def format_late_chunk_notice(task_id: str, *, worker_label: str = "OpenClaw") -> str:
    """User text arrived after execution already started; may not be in the current run."""
    if worker_label == "OpenClaw and Cursor":
        headline = "I received another message while OpenClaw and Cursor were already running for this task."
        meaning = "- Your latest text is saved on the task timeline, but the in-flight collaboration may not include it."
    elif worker_label == "Cursor":
        headline = "I received another message while Cursor was already running for this task."
        meaning = "- Your latest text is saved on the task timeline, but the in-flight Cursor run may not include it."
    else:
        headline = "I received another message while OpenClaw was already running for this task."
        meaning = "- Your latest text is saved on the task timeline, but the in-flight OpenClaw run may not include it."
    return "\n".join(
        [
            "Andrea:",
            headline,
            "",
            "What this means:",
            meaning,
            "- For a follow-up that must change execution, send a new request after this one finishes.",
            "",
            f"Task: `{task_id}`",
        ]
    )


def format_progress_message(
    task_id: str,
    *,
    progress_text: str,
    worker_label: str = "OpenClaw",
    routing_hint: str = "",
    collaboration_mode: str = "",
    provider: str = "",
    model: str = "",
    preferred_model_label: str = "",
) -> str:
    routing_note = _routing_note(routing_hint, collaboration_mode)
    headline = "Progress update."
    if worker_label == "OpenClaw and Cursor":
        headline = "OpenClaw and Cursor coordination update."
    elif worker_label == "OpenClaw":
        headline = "OpenClaw coordination update."
    elif worker_label == "Cursor":
        headline = "Cursor execution update."
    model_label = _model_label(provider, model)
    preferred_model_note = _preferred_model_note(preferred_model_label)
    lines = [
        "Andrea:",
        headline,
        "",
        "What happened:",
        f"- {_clip(_normalize_whitespace(progress_text), 700)}",
    ]
    if model_label:
        if worker_label == "Cursor":
            lines.append(f"- Active model: {model_label}")
        else:
            lines.append(f"- Active OpenClaw model: {model_label}")
    elif preferred_model_note:
        lines.append(preferred_model_note)
    if routing_note:
        lines.append(routing_note)
    lines.extend(["", *_footer_lines(task_id, "running")])
    return "\n".join(lines)


def format_direct_message(reply_text: str) -> str:
    scrubbed = strip_conversational_soft_failure_boilerplate(reply_text)
    safe = sanitize_user_surface_text(scrubbed, fallback=scrubbed, limit=4000)
    if not str(safe or "").strip():
        safe = _normalize_whitespace(scrubbed)
    clean = _normalize_whitespace(safe)
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
    provider: str = "",
    model: str = "",
    preferred_model_label: str = "",
) -> str:
    routing_note = _routing_note(routing_hint, collaboration_mode)
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
        *([routing_note] if routing_note else []),
        "",
    ]
    model_label = _model_label(provider, model)
    if model_label:
        if worker_label == "Cursor":
            lines.insert(len(lines) - 1, f"- Active model context: {model_label}.")
        else:
            lines.insert(len(lines) - 1, f"- OpenClaw is currently coordinating with {model_label}.")
    else:
        preferred_model_note = _preferred_model_note(preferred_model_label)
        if preferred_model_note:
            lines.insert(len(lines) - 1, preferred_model_note)
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
    visibility_mode: str = "",
    collaboration_trace: list[str] | None = None,
    routing_hint: str = "",
    collaboration_mode: str = "",
    provider: str = "",
    model: str = "",
    preferred_model_label: str = "",
) -> str:
    if backend == "openclaw" and worker_label == "Cursor":
        worker_label = "OpenClaw"
    summary_excerpt = _cursor_excerpt(summary)
    summary_sentence = _first_sentence(summary_excerpt)
    summary_key = surface_similarity_key(summary_excerpt)
    sentence_key = surface_similarity_key(summary_sentence)
    show_summary_block = bool(summary_excerpt)
    if summary_key and sentence_key and (
        summary_key == sentence_key
        or summary_key.startswith(sentence_key)
        or sentence_key.startswith(summary_key)
    ):
        show_summary_block = len(summary_excerpt) > max(len(summary_sentence) + 80, 240)
    completed = status == "completed"
    result_label = speaker_label = _speaker_section_label(
        worker_label=worker_label,
        delegated_to_cursor=delegated_to_cursor,
        provider=provider,
        model=model,
        preferred_model_label=preferred_model_label,
    )
    if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
        result_label = speaker_label
    if completed:
        soft_failure_summary = bool(SOFT_FAILURE_SUMMARY_RE.search(summary_sentence or summary_excerpt))
        if soft_failure_summary:
            andrea_line = (
                "I could not complete your request successfully, "
                "but I captured the safe failure summary below."
            )
        elif pr_url:
            if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
                andrea_line = "I finished your request and there is a PR ready to review."
            elif worker_label == "OpenClaw":
                andrea_line = "I finished your request and OpenClaw completed it successfully."
            else:
                andrea_line = "I finished your request and Cursor prepared a PR for review."
        elif summary_sentence:
            andrea_line = f"I finished your request. {summary_sentence}"
        else:
            andrea_line = "I finished your request and captured the result below."
    else:
        andrea_line = "I could not complete your request successfully, but I captured the safe failure summary below."

    if completed:
        if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
            happened_line = "- OpenClaw coordinated this task and Cursor finished the heavy execution."
        elif worker_label == "OpenClaw":
            happened_line = "- OpenClaw finished processing this task."
        else:
            happened_line = "- Cursor finished processing this task."
    else:
        if worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
            happened_line = "- OpenClaw and Cursor did not complete this task successfully."
        elif worker_label == "OpenClaw":
            happened_line = "- OpenClaw ended in a failed state for this task."
        else:
            happened_line = "- Cursor ended in a failed state for this task."

    lines = [
        "Andrea:",
        andrea_line,
        "",
        "What happened:",
        happened_line,
    ]
    if completed and pr_url:
        lines.append("- A PR is available for review.")
    elif completed and summary_sentence and not andrea_line.endswith(summary_sentence) and show_summary_block:
        lines.append(f"- Outcome: {summary_sentence}")
    elif not completed and last_error:
        lines.append(f"- Failure: {_clip(last_error, 220)}")
    routing_note = _routing_note(routing_hint, collaboration_mode)
    if routing_note:
        lines.append(routing_note)
    lines.extend(
        _collaboration_trace_lines(
            collaboration_trace,
            visibility_mode=visibility_mode,
            suppress_against=[summary_excerpt, summary_sentence],
        )
    )

    if show_summary_block:
        lines.extend(
            [
                "",
                f"{result_label} said:",
                summary_excerpt,
            ]
        )
    model_label = _model_label(provider, model)
    if model_label:
        lines.extend(["", f"OpenClaw model used: {model_label}"])
    else:
        preferred_model_note = _preferred_model_note(preferred_model_label)
        if preferred_model_note:
            note_body = preferred_model_note[2:] if preferred_model_note.startswith("- ") else preferred_model_note
            lines.extend(["", note_body])

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


def format_alexa_session_summary(
    task_id: str,
    *,
    status: str,
    request_text: str,
    summary: str = "",
    assistant_route: str = "",
    worker_label: str = "OpenClaw",
    delegated_to_cursor: bool = False,
    agent_url: str = "",
    pr_url: str = "",
    last_error: str = "",
) -> str:
    request_line = _clip(_normalize_whitespace(request_text), 220) or "Alexa request"
    summary_excerpt = _cursor_excerpt(summary, limit=500)
    summary_sentence = _first_sentence(summary_excerpt, limit=220)
    completed = status == "completed"
    handled_by = "Andrea directly"
    route = str(assistant_route or "").strip().lower()
    if route == "direct":
        handled_by = "Andrea directly"
    elif worker_label == "OpenClaw and Cursor" or delegated_to_cursor:
        handled_by = "OpenClaw with Cursor support"
    elif worker_label == "OpenClaw":
        handled_by = "OpenClaw"
    elif worker_label == "Cursor":
        handled_by = "Cursor"
    lines = [
        "Andrea:",
        "Alexa session summary.",
        "",
        "What you asked:",
        f"- {request_line}",
        "",
        "What happened:",
        f"- Handled by: {handled_by}",
        f"- Status: {status}",
    ]
    if completed and summary_sentence:
        lines.append(f"- Outcome: {summary_sentence}")
    elif not completed and last_error:
        lines.append(f"- Failure: {_clip(last_error, 220)}")
    if summary_excerpt:
        lines.extend(["", "Summary:", summary_excerpt])
    lines.extend(
        [
            "",
            *_footer_lines(
                task_id,
                status,
                agent_url=agent_url,
                pr_url=pr_url,
                last_error=last_error,
            ),
        ]
    )
    return "\n".join(lines)
