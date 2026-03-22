"""
Shared helpers for Cursor HTTP clients (used by cursor_openclaw and mirrored patterns).

Keep behavioral notes in sync with skills/cursor_handoff when changing retry semantics.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from urllib.parse import urlparse

# Cursor agent IDs are opaque strings; disallow path-like / URL-like values.
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
GITHUB_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Synthetic status for transport-layer failures (retried like 5xx).
TRANSIENT_TRANSPORT_STATUS = 599

USER_AGENT_OPENCLAW = "cursor-openclaw-integration/1.1"
USER_AGENT_HANDOFF = "openclaw-cursor-handoff/1.2"


def assert_no_newlines_or_nul(value: str, field_name: str) -> None:
    """Reject branch names and similar fields that could break argv or logs."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{field_name} cannot contain newlines or null bytes.")


def validate_agent_id(agent_id: str, flag_name: str = "--id") -> None:
    aid = (agent_id or "").strip()
    if not aid:
        raise ValueError(f"{flag_name} cannot be empty.")
    if not AGENT_ID_PATTERN.fullmatch(aid):
        raise ValueError(
            f"Invalid {flag_name} format (use only letters, digits, and ._:-). "
            "If you pasted a URL, pass only the agent id from the dashboard."
        )


def encode_request_json(body: Dict[str, Any]) -> bytes:
    """Serialize a dict to UTF-8 JSON for HTTP bodies (Unicode-safe)."""
    try:
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as err:
        raise ValueError(f"Request body is not JSON-serializable: {err}") from err


def parse_json_response_body(raw: str, max_preview: int = 2000) -> Dict[str, Any]:
    """Parse JSON from a successful HTTP body; never raise — return a structured dict."""
    if not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except json.JSONDecodeError:
        return {
            "_non_json_response": True,
            "body_preview": raw[:max_preview],
        }


def argv_has_json_flag(argv: list[str] | None = None) -> bool:
    import sys

    argv = argv or sys.argv
    return "--json" in argv


def redact_secret(value: str) -> str:
    """Short redacted preview for API keys (diagnostics only)."""
    if not value:
        return "***"
    if len(value) <= 8:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def parse_openai_enabled(raw: str | None = None) -> bool:
    """True when OPENAI_API_ENABLED is 1, true, or yes (case-insensitive)."""
    import os

    v = (raw if raw is not None else os.getenv("OPENAI_API_ENABLED", "")).strip().lower()
    return v in ("1", "true", "yes")


def normalize_github_repository_input(raw_value: str) -> str:
    """
    Normalize a GitHub repository input to canonical https URL form.

    Accepts either:
      - owner/repo
      - http(s)://github.com/owner/repo[/...]
    """
    raw = (raw_value or "").strip()
    if not raw:
        raise ValueError("Repository cannot be empty.")

    if GITHUB_SLUG_PATTERN.fullmatch(raw):
        return f"https://github.com/{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Repository must be a GitHub URL or owner/repo slug.")
    if (parsed.netloc or "").lower() != "github.com":
        raise ValueError("Repository host must be github.com.")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub repository URL must include owner and repo.")
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    slug = f"{owner}/{repo}"
    if not GITHUB_SLUG_PATTERN.fullmatch(slug):
        raise ValueError("Invalid GitHub repository format.")
    return f"https://github.com/{slug}"
