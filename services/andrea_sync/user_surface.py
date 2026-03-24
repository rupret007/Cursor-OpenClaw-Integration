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

# Long-lived OpenClaw / multi-agent transcripts sometimes leak into user-facing summaries.
# Keep this list tight to avoid false positives on normal prose.
STALE_OPENCLAW_HANDOFF_RE = re.compile(
    r"\b("
    r"extreme\s+masterclass|self-improvement\s+sprint|delegated\s+the\s+task|"
    r"multi-?agent\s+handoff|spawned\s+(?:a\s+)?(?:new\s+)?session|stale\s+sprint"
    r")\b",
    re.I,
)

# Provider / memory subsystem errors that should not surface as "answers" on lightweight asks.
MEMORY_PROVIDER_LEAK_RE = re.compile(
    r"\b("
    r"embedding\s+quota|"
    r"memory\s+quota\s+(?:exceeded|hit|reached|error)|"
    r"vector\s+(?:store|database|db)\s+(?:error|unavailable|quota)|"
    r"active\s+context\s+(?:overflow|exceeded)|"
    r"context\s+window\s+exceeded|"
    r"token\s+limit\s+exceeded|"
    r"rate\s+limit(?:ed)?\s+(?:for\s+)?(?:embeddings?|vectors?)"
    r")\b",
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


def is_stale_openclaw_narrative(text: Any) -> bool:
    """True when text looks like internal runtime chatter or unrelated multi-agent session recap."""
    normalized = normalize_whitespace(text)
    if not normalized:
        return False
    if is_internal_runtime_text(normalized):
        return True
    if STALE_OPENCLAW_HANDOFF_RE.search(normalized):
        return True
    if MEMORY_PROVIDER_LEAK_RE.search(normalized):
        return True
    return False


# Lifecycle / terminal notifications sometimes reuse this canned line; strip it from
# conversational direct replies so recall questions do not sound like hard failures.
SOFT_FAILURE_BOILERPLATE_RE = re.compile(
    r"I\s+could\s+not\s+complete\s+your\s+request\s+successfully[^.\n]*(?:\.|$)",
    re.I,
)
SOFT_FAILURE_CAPTURE_TAIL_RE = re.compile(
    r"(?:,\s*)?but\s+I\s+captured\s+the\s+safe\s+failure\s+summary\s+below\.?",
    re.I,
)


def strip_conversational_soft_failure_boilerplate(text: Any) -> str:
    """Remove terminal-style soft-failure boilerplate from normal assistant / direct surfaces."""
    raw = str(text or "")
    cleaned = SOFT_FAILURE_BOILERPLATE_RE.sub("", raw)
    cleaned = SOFT_FAILURE_CAPTURE_TAIL_RE.sub("", cleaned)
    return normalize_whitespace(cleaned)


def sanitize_user_surface_text(text: Any, *, fallback: Any = "", limit: int = 500) -> str:
    safe_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        stripped = normalize_whitespace(str(raw_line).lstrip("-*• "))
        if not stripped or is_internal_runtime_text(stripped) or is_stale_openclaw_narrative(stripped):
            continue
        safe_lines.append(stripped)
    collapsed = normalize_whitespace(" ".join(safe_lines))
    if collapsed:
        return clip_text(collapsed, limit)
    backup = normalize_whitespace(fallback)
    if (
        backup
        and not is_internal_runtime_text(backup)
        and not is_stale_openclaw_narrative(backup)
    ):
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


def format_scenario_proof_receipt(
    *,
    scenario_id: str,
    scenario_label: str,
    verified: bool,
    proof_summary: str,
    next_step: str = "",
    remaining_risks: Iterable[str] | None = None,
) -> str:
    """User-facing proof receipt lines (calm, non-runtime)."""
    label = clip_text(scenario_label or scenario_id, 120)
    status = "Verified" if verified else "Not fully verified yet"
    lines = [
        f"**Job type:** {label} (`{clip_text(scenario_id, 80)}`)",
        f"**Proof status:** {status}",
    ]
    ps = clip_text(proof_summary, 900)
    if ps:
        lines.append(f"**What was checked:** {ps}")
    risks = list(remaining_risks or [])[:6]
    if risks:
        lines.append("**Still open:** " + "; ".join(clip_text(r, 160) for r in risks))
    ns = clip_text(next_step, 400)
    if ns:
        lines.append(f"**Next safe step:** {ns}")
    return "\n".join(lines)
