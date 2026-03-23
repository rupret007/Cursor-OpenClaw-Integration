"""Deterministic failure / error taxonomy (Phase 4)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

_PATTERNS: List[Tuple[str, str]] = [
    (r"\b401\b|unauthorized|invalid.?token|auth", "auth"),
    (r"\btimeout\b|timed out|deadline", "transport_timeout"),
    (r"\bconnection refused\b|econnrefused|network", "transport_network"),
    (r"\bschema\b|validation|invalid payload", "schema"),
    (r"\bpolicy\b|forbidden|blocked", "policy"),
    (r"\bratelimit|429\b", "rate_limit"),
    (r"\b500\b|internal server error", "provider_error"),
]


def classify_error(message: str) -> str:
    m = (message or "").lower()
    for pattern, label in _PATTERNS:
        if re.search(pattern, m, re.I):
            return label
    return "unknown"


def classify_exception(exc: BaseException) -> str:
    return classify_error(str(exc))


def failure_dict(message: str, *, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "category": classify_error(message),
        "message": (message or "")[:2000],
        "context": context or {},
    }
