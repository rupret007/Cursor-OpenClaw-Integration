"""Assistant-wide recovery suggestions (Phase 4; complements repair_orchestrator)."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .failure_classifier import classify_error


RecoveryAction = Tuple[str, str]  # action, detail


def suggest_recovery(category: str) -> List[RecoveryAction]:
    c = (category or "").strip().lower()
    if c == "auth":
        return [("reauth", "Refresh credentials or reconnect the integration.")]
    if c == "transport_timeout":
        return [("retry", "Retry with backoff."), ("fallback", "Try alternate provider if configured.")]
    if c == "transport_network":
        return [("retry", "Check network connectivity."), ("degrade", "Return partial results if possible.")]
    if c == "rate_limit":
        return [("backoff", "Wait and retry with jitter.")]
    if c == "schema":
        return [("repair_payload", "Fix arguments and re-submit.")]
    if c == "policy":
        return [("escalate", "Request human approval or adjust policy.")]
    if c == "provider_error":
        return [("retry", "Retry once."), ("fallback", "Fail over to backup model/provider.")]
    return [("inspect", "Collect logs and classify further.")]


def recovery_plan_from_message(message: str) -> Dict[str, Any]:
    cat = classify_error(message)
    steps = [{"action": a, "detail": d} for a, d in suggest_recovery(cat)]
    return {"category": cat, "steps": steps}


def record_recovery_attempt_event_payload(
    *,
    phase: str,
    action: str,
    detail: str,
) -> Dict[str, Any]:
    return {"phase": phase, "action": action, "detail": detail}


def recovery_branches_for_plan_step(*, failure_category: str) -> List[Dict[str, str]]:
    """Structured recovery options stored on plan steps / surfaced to operators."""
    return [{"action": a, "detail": d} for a, d in suggest_recovery(failure_category)]
