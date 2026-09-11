"""
Shared helpers for Cursor HTTP clients.

Mirror of scripts/cursor_api_common.py in the integration repo — keep copies in sync.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

# Cursor agent IDs are opaque strings; disallow path-like / URL-like values.
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

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


TERMINAL_AGENT_STATUSES = frozenset({"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"})
TERMINAL_FAILURE_AGENT_STATUSES = TERMINAL_AGENT_STATUSES - {"FINISHED"}
NON_TERMINAL_AGENT_STATUSES = frozenset({"CREATING", "PENDING", "RUNNING"})


def agent_status_readout(response: Any, expected_id: str) -> Dict[str, Any]:
    """Separate the observed Cloud agent state from a successful HTTP status request.

    A 200 from the status endpoint only proves the endpoint answered; it says
    nothing about whether the agent itself is healthy. Callers must surface
    `agent_status` / `status_verified` / `next_action` alongside `ok` so a
    FAILED, still-CREATING, or unrecognized agent is never read as done just
    because the HTTP call succeeded.
    """
    state = "UNKNOWN"
    if isinstance(response, dict) and not response.get("_non_json_response"):
        raw_state = response.get("status")
        if response.get("id") == expected_id and isinstance(raw_state, str):
            candidate = raw_state.strip().upper()
            if candidate in TERMINAL_AGENT_STATUSES | NON_TERMINAL_AGENT_STATUSES:
                state = candidate

    if state == "CREATING":
        next_action = "Agent is being created. Check this agent again; do not submit a duplicate."
    elif state == "PENDING":
        next_action = "Agent is queued. Check this agent again; do not submit a duplicate."
    elif state == "RUNNING":
        next_action = "Work is still running. Check this agent again; no completion is confirmed."
    elif state == "FINISHED":
        next_action = (
            "Agent reports finished. Review its conversation and artifacts; "
            "changes are not verified by this status check."
        )
    elif state in TERMINAL_FAILURE_AGENT_STATUSES:
        next_action = (
            "Agent stopped without completing the handoff. Inspect its conversation and artifacts "
            "before deciding whether to retry."
        )
    else:
        next_action = (
            "Status is unverified. Check this agent ID and inspect the existing agent before any retry."
        )
    return {"agent_status": state, "status_verified": state != "UNKNOWN", "next_action": next_action}


def allowlisted_agent_snapshot(agent: Any, *, expected_id: str = "") -> Dict[str, str]:
    """Return id + status only. Never conversation, URLs, or raw bodies."""
    aid = str(expected_id or "").strip()
    status = ""
    if isinstance(agent, dict):
        raw_id = str(agent.get("id") or "").strip()
        if raw_id:
            aid = raw_id
        status = str(agent.get("status") or "").strip()
    return {"id": aid, "status": status}


def classify_followup_agent(
    status_code: int,
    agent: Any,
    *,
    expected_id: str,
) -> tuple[str, str | None, Dict[str, str]]:
    """Classify whether a live followup POST may proceed.

    agent_state is one of: running, missing, stale, unknown.
    A non-None block reason means the followup must not be sent.
    """
    expected = str(expected_id or "").strip()
    snapshot = allowlisted_agent_snapshot(agent, expected_id=expected)
    if int(status_code) == 404:
        return (
            "missing",
            (
                "Followup is blocked because that Cloud agent was not found. "
                "The followup was not sent."
            ),
            snapshot,
        )
    if int(status_code) >= 400:
        return (
            "unknown",
            (
                "Followup is blocked because the Cloud agent status check failed "
                f"(HTTP {int(status_code)}). The followup was not sent."
            ),
            snapshot,
        )
    if not isinstance(agent, dict) or agent.get("_non_json_response"):
        return (
            "missing",
            "Followup is blocked because the Cloud agent status was empty. "
            "The followup was not sent.",
            snapshot,
        )
    raw_id = str(agent.get("id") or "").strip()
    if raw_id and expected and raw_id != expected:
        return (
            "unknown",
            (
                "Followup is blocked because the status response did not match "
                "the requested agent. The followup was not sent."
            ),
            snapshot,
        )
    raw_status = str(agent.get("status") or "").strip()
    if not raw_status:
        return (
            "missing",
            "Followup is blocked because the Cloud agent has no status. "
            "The followup was not sent.",
            snapshot,
        )
    if raw_status.upper() in TERMINAL_AGENT_STATUSES:
        return (
            "stale",
            (
                f"Followup is blocked because Cloud agent {expected or raw_id} "
                f"is {raw_status} (not a running agent). The followup was not sent."
            ),
            snapshot,
        )
    return "running", None, snapshot


FOLLOWUP_AGENT_NOT_CHECKED = (
    "Followup dry-run did not check Cloud agent status. The followup was not sent."
)


def followup_dry_run_block_reason(receipt_block: str | None) -> str:
    """Dry-run never proves a followup is clear: agent status is not GET."""
    text = str(receipt_block or "").strip()
    return text or FOLLOWUP_AGENT_NOT_CHECKED


def followup_dry_run_fields(receipt_block: str | None) -> Dict[str, Any]:
    """Allowlisted dry-run followup receipt. Never implies the POST is clear."""
    return {
        "agent_state": "not_checked",
        "receipt_would_block": receipt_block,
        "followup_would_block": followup_dry_run_block_reason(receipt_block),
        "followup_ready": False,
    }


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
