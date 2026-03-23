"""Shared helpers for calm, user-safe runtime text."""
from __future__ import annotations

import re
from typing import Any, Iterable, List

INTERNAL_RUNTIME_RE = re.compile(
    r"\b("
    r"sessionkey|session key|sessionid|session id|session label|runtime id|"
    r"sessions_send|sessions_spawn|attachments\.enabled|tool chatter|tool call|"
    r"internal runtime|cursor session|label that identifies|session identifier|"
    r"openclaw skills install|openclaw skills update|skills info|gateway restart|"
    r"blockedbyallowlist|missing_(?:bins|env|config|os)|"
    r"eligible(?::|=)\s*(?:true|false)|--session-id"
    r")\b|(?:plugins\.entries|channels\.)[\w.-]+",
    re.I,
)


def clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def normalize_whitespace(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def surface_similarity_key(value: Any) -> str:
    normalized = normalize_whitespace(value).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def is_internal_runtime_text(text: Any) -> bool:
    normalized = normalize_whitespace(text)
    if not normalized:
        return False
    if INTERNAL_RUNTIME_RE.search(normalized):
        return True
    return bool(
        re.search(r"\b(tool|runtime|config|setting|session)\b", normalized, re.I)
        and re.search(r"\b(key|label|id|enabled|disabled|missing|required)\b", normalized, re.I)
    )


def sanitize_user_surface_text(text: Any, *, fallback: Any = "", limit: int = 500) -> str:
    safe_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        stripped = normalize_whitespace(str(raw_line).lstrip("-*• "))
        if not stripped or is_internal_runtime_text(stripped):
            continue
        safe_lines.append(stripped)
    collapsed = normalize_whitespace(" ".join(safe_lines))
    if collapsed:
        return clip_text(collapsed, limit)
    backup = normalize_whitespace(fallback)
    if backup and not is_internal_runtime_text(backup):
        return clip_text(backup, limit)
    return ""


def dedupe_user_surface_items(
    items: Iterable[Any],
    *,
    limit: int = 4,
    item_limit: int = 240,
    suppress_against: Iterable[Any] | Any = (),
) -> List[str]:
    suppress_values: List[Any]
    if isinstance(suppress_against, (str, bytes)) or not isinstance(suppress_against, Iterable):
        suppress_values = [suppress_against]
    else:
        suppress_values = list(suppress_against)
    suppressed: List[str] = []
    for raw in suppress_values:
        text = sanitize_user_surface_text(raw, limit=item_limit)
        key = surface_similarity_key(text)
        if key and key not in suppressed:
            suppressed.append(key)
    out: List[str] = []
    seen: List[str] = []
    for raw in items:
        text = sanitize_user_surface_text(raw, limit=item_limit)
        if not text:
            continue
        key = surface_similarity_key(text)
        if not key:
            continue
        if key in seen:
            continue
        if any(key in other or other in key for other in seen if len(other) >= 24 and len(key) >= 24):
            continue
        if any(key in other or other in key for other in suppressed if len(other) >= 24 and len(key) >= 24):
            continue
        out.append(text)
        seen.append(key)
        if len(out) >= limit:
            break
    return out
