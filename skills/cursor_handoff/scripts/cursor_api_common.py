"""
Shared helpers for Cursor HTTP clients.

Mirror of scripts/cursor_api_common.py in the integration repo — keep copies in sync.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
TRANSIENT_TRANSPORT_STATUS = 599
USER_AGENT_OPENCLAW = "cursor-openclaw-integration/1.1"
USER_AGENT_HANDOFF = "openclaw-cursor-handoff/1.2"


def validate_agent_id(agent_id: str, flag_name: str = "--id") -> None:
    aid = (agent_id or "").strip()
    if not aid:
        raise ValueError(f"{flag_name} cannot be empty.")
    if not AGENT_ID_PATTERN.fullmatch(aid):
        raise ValueError(
            f"Invalid {flag_name} format (use only letters, digits, and ._:-). "
            "If you pasted a URL, pass only the agent id from the dashboard."
        )


def parse_json_response_body(raw: str, max_preview: int = 2000) -> Dict[str, Any]:
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
