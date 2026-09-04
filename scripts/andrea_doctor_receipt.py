#!/usr/bin/env python3
"""Build a stable, redaction-safe handoff receipt from the offline doctor.

The receipt intentionally contains only stage outcomes and the code-owned
readiness contract. Raw probe output, environment values, and capability notes
are never copied into the artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
KIND = "andrea_offline_doctor_receipt"
ALLOWED_STAGE_STATUSES = {
    "not_run",
    "passed",
    "failed",
    "blocked",
    "ready",
    "ready_with_limits",
    "skipped_offline",
}
DEFAULT_HOLDS = [
    "Do not send any live message.",
    "Keep Private API off.",
    "Do not live-send BlueBubbles.",
    "Do not write credentials into the repository.",
    "Do not merge, tag, deploy, or restart a gateway unless the owner explicitly asks.",
]
FALLBACK_ACTION = (
    "Restore a valid readiness receipt before autonomous operation, then rerun "
    "bash scripts/andrea_doctor.sh --offline."
)
RERUN_COMMAND = (
    "bash scripts/andrea_doctor.sh --offline --receipt /tmp/andrea-doctor-receipt.json"
)


def _stage_status(value: str) -> str:
    return value if value in ALLOWED_STAGE_STATUSES else "failed"


def _safe_string(value: Any, fallback: str = "", *, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    return cleaned[:limit] or fallback


def _safe_string_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    cleaned = [_safe_string(item) for item in value]
    cleaned = [item for item in cleaned if item]
    return cleaned[:20] or list(fallback)


def _safe_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        actions.append(
            {
                "id": _safe_string(item.get("id"), "capability", limit=120),
                "status": _stage_status(_safe_string(item.get("status"), "blocked")),
                "critical": bool(item.get("critical")),
                "action": _safe_string(item.get("action"), FALLBACK_ACTION),
            }
        )
    return actions


def _read_readiness(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_receipt(
    readiness: dict[str, Any],
    *,
    security_status: str,
    reliability_status: str,
    openclaw_status: str,
    exit_code: int,
) -> dict[str, Any]:
    """Return the allowlisted receipt; never copy raw probe or capability data."""
    grade = _safe_string(readiness.get("grade"), "C", limit=1)
    if grade not in {"A", "B", "C"}:
        grade = "C"
    plan = readiness.get("readiness_plan")
    if not isinstance(plan, dict):
        plan = {}

    security = _stage_status(security_status)
    reliability = _stage_status(reliability_status)
    openclaw = _stage_status(openclaw_status)
    readiness_status = {"A": "ready", "B": "ready_with_limits", "C": "blocked"}[grade]
    failed_stage = security != "passed" or reliability != "passed"
    if exit_code != 0 or grade == "C" or failed_stage:
        overall_status = "blocked"
    elif grade == "B":
        overall_status = "ready_with_limits"
    else:
        overall_status = "ready"

    who_acts_first = _safe_string(plan.get("who_acts_first"), "owner", limit=40)
    if who_acts_first not in {"owner", "coding_agent"}:
        who_acts_first = "owner"
    next_action = _safe_string(plan.get("next_action"), FALLBACK_ACTION)
    routing = plan.get("routing") if isinstance(plan.get("routing"), dict) else {}

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "mode": "offline",
        "overall_status": overall_status,
        "exit_code": int(exit_code),
        "stages": {
            "security": {"status": security},
            "readiness": {"status": readiness_status, "grade": grade},
            "reliability": {"status": reliability},
            "openclaw_probe": {"status": openclaw},
        },
        "handoff": {
            "safe_for_autonomous_ops": bool(plan.get("safe_for_autonomous_ops"))
            and overall_status != "blocked",
            "blocker_count": max(0, int(plan.get("blocker_count") or 0)),
            "who_acts_first": who_acts_first,
            "next_action": next_action,
            "andrea_next_action": _safe_string(
                plan.get("andrea_next_action"), FALLBACK_ACTION
            ),
            "coding_agent_next_action": _safe_string(
                plan.get("coding_agent_next_action"), FALLBACK_ACTION
            ),
            "owner_next_action": _safe_string(
                plan.get("owner_next_action"), next_action
            ),
            "holds": _safe_string_list(plan.get("holds"), DEFAULT_HOLDS),
            "routing": {
                "andrea": _safe_string(routing.get("andrea"), "offline only"),
                "coding_agent": _safe_string(
                    routing.get("coding_agent"), "offline code and tests only"
                ),
                "owner": _safe_string(routing.get("owner"), "owner-gated actions only"),
            },
            "actions": _safe_actions(plan.get("actions")),
        },
        "commands": {"rerun": RERUN_COMMAND},
    }
    fingerprint_basis = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    receipt["receipt_fingerprint"] = hashlib.sha256(fingerprint_basis).hexdigest()
    return receipt


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    """Atomically replace the requested artifact and keep it operator-private."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a safe offline doctor receipt")
    parser.add_argument("--readiness-json", type=Path)
    parser.add_argument("--security-status", required=True)
    parser.add_argument("--reliability-status", required=True)
    parser.add_argument("--openclaw-status", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    receipt = build_receipt(
        _read_readiness(args.readiness_json),
        security_status=args.security_status,
        reliability_status=args.reliability_status,
        openclaw_status=args.openclaw_status,
        exit_code=args.exit_code,
    )
    write_receipt(args.output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
