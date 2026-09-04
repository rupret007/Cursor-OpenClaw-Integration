#!/usr/bin/env python3
"""Build and consume a stable, redaction-safe offline doctor handoff receipt.

The receipt intentionally contains only stage outcomes and the code-owned
readiness contract. Raw probe output, environment values, and capability notes
are never copied into the artifact.

Consumers (Bob, Codex, Grok, Claude, Andrea, dashboards, and scripts) must
load the artifact through this module. Verify recomputes the fingerprint,
rejects unknown keys, and fail-closes to an owner-blocked packet when a
stage is not passed — even if leftover Grade A next-step text is still
present on an older receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
KIND = "andrea_offline_doctor_receipt"
PACKET_KIND = "andrea_offline_doctor_audience_packet"
ALLOWED_STAGE_STATUSES = {
    "not_run",
    "passed",
    "failed",
    "blocked",
    "ready",
    "ready_with_limits",
    "skipped_offline",
}
ALLOWED_OVERALL = {"ready", "ready_with_limits", "blocked"}
ALLOWED_WHO = {"owner", "coding_agent"}
GATE_STAGES = ("security", "reliability")
SCHEMA_1_TOP_LEVEL = {
    "schema_version",
    "kind",
    "mode",
    "overall_status",
    "exit_code",
    "stages",
    "handoff",
    "commands",
    "receipt_fingerprint",
}
SCHEMA_2_TOP_LEVEL = SCHEMA_1_TOP_LEVEL | {"blocked_reason", "failed_stages"}
SCHEMA_1_HANDOFF = {
    "safe_for_autonomous_ops",
    "blocker_count",
    "who_acts_first",
    "next_action",
    "andrea_next_action",
    "coding_agent_next_action",
    "owner_next_action",
    "holds",
    "routing",
    "actions",
}
SCHEMA_2_HANDOFF = SCHEMA_1_HANDOFF | {"blocked_reason", "failed_stages"}
AUDIENCE_ALIASES = {
    "andrea": "andrea",
    "coding_agent": "coding_agent",
    "bob": "coding_agent",
    "codex": "coding_agent",
    "grok": "coding_agent",
    "claude": "coding_agent",
    "owner": "owner",
    "dashboard": "dashboard",
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
CONSUME_COMMAND = (
    "python3 scripts/andrea_doctor_receipt.py --consume "
    "/tmp/andrea-doctor-receipt.json --audience coding_agent"
)
VERIFY_COMMAND = "python3 scripts/andrea_doctor_receipt.py --verify /tmp/andrea-doctor-receipt.json"
DEFAULT_ROUTING = {
    "andrea": "offline only",
    "coding_agent": "offline code and tests only",
    "owner": "owner-gated actions only",
}
STAGE_FAILED_ANDREA = (
    "Do not run autonomous or live communication work. Keep outbound drafts "
    "pending. Wait for the owner to restore the failed doctor stage, then rerun "
    "bash scripts/andrea_doctor.sh --offline."
)
STAGE_FAILED_CODING_AGENT = (
    "Stay offline. Do not treat this host as ready. Do not install skills, "
    "restart OpenClaw, mutate credentials, or send messages. Report the failed "
    "doctor stage to the owner and wait."
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


def failed_gate_stages(security: str, reliability: str) -> list[str]:
    """Return security/reliability stages that are not a clean pass."""
    return [
        name
        for name, status in (("security", security), ("reliability", reliability))
        if status != "passed"
    ]


def blocked_reason_for(
    *,
    failed_stages: list[str],
    grade: str,
    exit_code: int,
) -> str:
    if failed_stages:
        return "stage_failed:" + ",".join(failed_stages)
    if grade == "C":
        return "grade_c"
    if exit_code != 0:
        return "nonzero_exit"
    return ""


def stage_failed_next_action(failed_stages: list[str]) -> str:
    names = ", ".join(stage for stage in failed_stages if stage in GATE_STAGES) or "doctor"
    return (
        f"Doctor stage failed ({names}). Stop autonomous operation. The owner "
        "must restore the failed stage, then rerun "
        "bash scripts/andrea_doctor.sh --offline --receipt "
        "/tmp/andrea-doctor-receipt.json."
    )


def stage_failed_owner_action(failed_stages: list[str]) -> str:
    names = ", ".join(stage for stage in failed_stages if stage in GATE_STAGES) or "doctor"
    return (
        f"Restore the failed doctor stage ({names}), then rerun "
        "bash scripts/andrea_doctor.sh --offline --receipt "
        "/tmp/andrea-doctor-receipt.json."
    )


def apply_failed_stage_contract(
    handoff: dict[str, Any], failed_stages: list[str]
) -> dict[str, Any]:
    """Owner-first override so leftover Grade A text cannot authorize work."""
    if not failed_stages:
        return handoff
    next_action = stage_failed_next_action(failed_stages)
    owner_action = stage_failed_owner_action(failed_stages)
    stage_actions = [
        {
            "id": f"doctor:{stage}",
            "status": "failed",
            "critical": True,
            "action": owner_action,
        }
        for stage in failed_stages
        if stage in GATE_STAGES
    ]
    existing = [
        item
        for item in _safe_actions(handoff.get("actions"))
        if not str(item.get("id") or "").startswith("doctor:")
    ]
    handoff["safe_for_autonomous_ops"] = False
    handoff["who_acts_first"] = "owner"
    handoff["next_action"] = next_action
    handoff["andrea_next_action"] = STAGE_FAILED_ANDREA
    handoff["coding_agent_next_action"] = STAGE_FAILED_CODING_AGENT
    handoff["owner_next_action"] = owner_action
    handoff["actions"] = (stage_actions + existing)[:20]
    return handoff


def _fingerprint_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}


def compute_fingerprint(receipt: dict[str, Any]) -> str:
    basis = json.dumps(
        _fingerprint_payload(receipt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(basis).hexdigest()


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
    failed_stages = failed_gate_stages(security, reliability)
    failed_stage = bool(failed_stages)
    if exit_code != 0 or grade == "C" or failed_stage:
        overall_status = "blocked"
    elif grade == "B":
        overall_status = "ready_with_limits"
    else:
        overall_status = "ready"
    blocked_reason = blocked_reason_for(
        failed_stages=failed_stages, grade=grade, exit_code=exit_code
    )

    who_acts_first = _safe_string(plan.get("who_acts_first"), "owner", limit=40)
    if who_acts_first not in ALLOWED_WHO:
        who_acts_first = "owner"
    next_action = _safe_string(plan.get("next_action"), FALLBACK_ACTION)
    routing = plan.get("routing") if isinstance(plan.get("routing"), dict) else {}

    handoff: dict[str, Any] = {
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
            "andrea": _safe_string(routing.get("andrea"), DEFAULT_ROUTING["andrea"]),
            "coding_agent": _safe_string(
                routing.get("coding_agent"), DEFAULT_ROUTING["coding_agent"]
            ),
            "owner": _safe_string(routing.get("owner"), DEFAULT_ROUTING["owner"]),
        },
        "actions": _safe_actions(plan.get("actions")),
    }
    apply_failed_stage_contract(handoff, failed_stages)
    handoff["blocked_reason"] = blocked_reason
    handoff["failed_stages"] = list(failed_stages)

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "mode": "offline",
        "overall_status": overall_status,
        "exit_code": int(exit_code),
        "blocked_reason": blocked_reason,
        "failed_stages": list(failed_stages),
        "stages": {
            "security": {"status": security},
            "readiness": {"status": readiness_status, "grade": grade},
            "reliability": {"status": reliability},
            "openclaw_probe": {"status": openclaw},
        },
        "handoff": handoff,
        "commands": {
            "rerun": RERUN_COMMAND,
            "verify": VERIFY_COMMAND,
            "consume": CONSUME_COMMAND,
        },
    }
    receipt["receipt_fingerprint"] = compute_fingerprint(receipt)
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


def _stage_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return raw if all(isinstance(value, dict) for value in raw.values()) else {}


def _handoff_map(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _unexpected_keys(payload: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(key for key in payload if key not in allowed)


def _fallback_packet(
    *,
    audience: str,
    requested: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PACKET_KIND,
        "audience": audience if audience in {"andrea", "coding_agent", "owner", "dashboard"} else "owner",
        "audience_requested": _safe_string(requested, "owner", limit=40),
        "trusted_receipt": False,
        "overall_status": "blocked",
        "blocked_reason": "invalid_receipt",
        "failed_stages": [],
        "grade": "C",
        "who_acts_first": "owner",
        "safe_for_autonomous_ops": False,
        "may_continue_offline_code": False,
        "must_wait_for_owner": True,
        "next_action": FALLBACK_ACTION,
        "andrea_next_action": STAGE_FAILED_ANDREA,
        "coding_agent_next_action": STAGE_FAILED_CODING_AGENT,
        "owner_next_action": FALLBACK_ACTION,
        "holds": list(DEFAULT_HOLDS),
        "routing": dict(DEFAULT_ROUTING),
        "actions": [],
        "commands": {
            "rerun": RERUN_COMMAND,
            "verify": VERIFY_COMMAND,
            "consume": CONSUME_COMMAND,
        },
        "receipt_fingerprint": "",
        "reason": _safe_string(reason, "invalid_receipt", limit=200),
    }


def verify_receipt(receipt: Any) -> tuple[bool, str, dict[str, Any]]:
    """Return (ok, reason, receipt). On failure the receipt is empty."""
    if not isinstance(receipt, dict):
        return False, "not_an_object", {}
    schema = receipt.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        return False, "unsupported_schema", {}
    allowed_top = SCHEMA_1_TOP_LEVEL if schema == 1 else SCHEMA_2_TOP_LEVEL
    extra = _unexpected_keys(receipt, allowed_top)
    if extra:
        return False, "unexpected_keys", {}
    if receipt.get("kind") != KIND:
        return False, "invalid_kind", {}
    if receipt.get("mode") != "offline":
        return False, "invalid_mode", {}
    stored_fp = receipt.get("receipt_fingerprint")
    if not isinstance(stored_fp, str) or len(stored_fp) != 64:
        return False, "invalid_fingerprint", {}
    if compute_fingerprint(receipt) != stored_fp:
        return False, "fingerprint_mismatch", {}
    overall = receipt.get("overall_status")
    if overall not in ALLOWED_OVERALL:
        return False, "invalid_overall_status", {}
    try:
        exit_code = int(receipt.get("exit_code"))
    except (TypeError, ValueError):
        return False, "invalid_exit_code", {}
    stages = _stage_map(receipt.get("stages"))
    required_stages = {"security", "readiness", "reliability", "openclaw_probe"}
    if set(stages) != required_stages:
        return False, "invalid_stages", {}
    for name in required_stages:
        status = stages[name].get("status")
        if status not in ALLOWED_STAGE_STATUSES:
            return False, "invalid_stage_status", {}
        extra_stage = _unexpected_keys(
            stages[name], {"status", "grade"} if name == "readiness" else {"status"}
        )
        if extra_stage:
            return False, "unexpected_stage_keys", {}
    readiness = stages["readiness"]
    grade = readiness.get("grade")
    if grade not in {"A", "B", "C"}:
        return False, "invalid_grade", {}
    expected_readiness = {"A": "ready", "B": "ready_with_limits", "C": "blocked"}[grade]
    if readiness.get("status") != expected_readiness:
        return False, "readiness_status_mismatch", {}
    security = str(stages["security"]["status"])
    reliability = str(stages["reliability"]["status"])
    computed_failed = failed_gate_stages(security, reliability)
    computed_overall = (
        "blocked"
        if exit_code != 0 or grade == "C" or computed_failed
        else "ready_with_limits"
        if grade == "B"
        else "ready"
    )
    if overall != computed_overall:
        return False, "overall_status_mismatch", {}
    computed_reason = blocked_reason_for(
        failed_stages=computed_failed, grade=grade, exit_code=exit_code
    )
    handoff = _handoff_map(receipt.get("handoff"))
    allowed_handoff = SCHEMA_1_HANDOFF if schema == 1 else SCHEMA_2_HANDOFF
    if _unexpected_keys(handoff, allowed_handoff):
        return False, "unexpected_handoff_keys", {}
    if schema == 2:
        if receipt.get("blocked_reason") != computed_reason:
            return False, "blocked_reason_mismatch", {}
        if receipt.get("failed_stages") != computed_failed:
            return False, "failed_stages_mismatch", {}
        if handoff.get("blocked_reason") != computed_reason:
            return False, "handoff_blocked_reason_mismatch", {}
        if handoff.get("failed_stages") != computed_failed:
            return False, "handoff_failed_stages_mismatch", {}
    if computed_overall == "blocked" and handoff.get("safe_for_autonomous_ops"):
        return False, "safe_flag_mismatch", {}
    who = handoff.get("who_acts_first")
    if who not in ALLOWED_WHO:
        return False, "invalid_who_acts_first", {}
    if schema == 2 and computed_failed:
        if who != "owner":
            return False, "who_acts_first_mismatch", {}
        if handoff.get("next_action") != stage_failed_next_action(computed_failed):
            return False, "next_action_mismatch", {}
        if handoff.get("andrea_next_action") != STAGE_FAILED_ANDREA:
            return False, "andrea_next_action_mismatch", {}
        if handoff.get("coding_agent_next_action") != STAGE_FAILED_CODING_AGENT:
            return False, "coding_agent_next_action_mismatch", {}
    commands = receipt.get("commands")
    if not isinstance(commands, dict) or _unexpected_keys(
        commands, {"rerun", "verify", "consume"} if schema == 2 else {"rerun"}
    ):
        return False, "invalid_commands", {}
    return True, "ok", receipt


def load_receipt(path: Path) -> tuple[bool, str, dict[str, Any]]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "missing_file", {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False, "unreadable_json", {}
    return verify_receipt(payload)


def _canonical_audience(value: str) -> str | None:
    return AUDIENCE_ALIASES.get(_safe_string(value, "", limit=40).lower())


def audience_packet(
    receipt: dict[str, Any],
    audience: str,
    *,
    trusted: bool = True,
    reason: str = "ok",
) -> dict[str, Any]:
    """Allowlisted next-step packet for one consumer. Never copies raw extras."""
    requested = _safe_string(audience, "owner", limit=40)
    canonical = _canonical_audience(requested)
    if canonical is None:
        return _fallback_packet(
            audience="owner", requested=requested, reason="invalid_audience"
        )
    if not trusted:
        return _fallback_packet(
            audience=canonical, requested=requested, reason=reason
        )

    stages = _stage_map(receipt.get("stages"))
    grade = _safe_string(stages.get("readiness", {}).get("grade"), "C", limit=1)
    if grade not in {"A", "B", "C"}:
        grade = "C"
    security = _stage_status(_safe_string(stages.get("security", {}).get("status"), "failed"))
    reliability = _stage_status(
        _safe_string(stages.get("reliability", {}).get("status"), "failed")
    )
    failed_stages = failed_gate_stages(security, reliability)
    try:
        exit_code = int(receipt.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 1
    blocked_reason = blocked_reason_for(
        failed_stages=failed_stages, grade=grade, exit_code=exit_code
    )
    overall = receipt.get("overall_status")
    if overall not in ALLOWED_OVERALL:
        overall = "blocked"

    handoff = dict(_handoff_map(receipt.get("handoff")))
    apply_failed_stage_contract(handoff, failed_stages)
    who = _safe_string(handoff.get("who_acts_first"), "owner", limit=40)
    if who not in ALLOWED_WHO:
        who = "owner"
    if failed_stages or grade == "C" or exit_code != 0:
        who = "owner"
    next_by_audience = {
        "andrea": _safe_string(handoff.get("andrea_next_action"), FALLBACK_ACTION),
        "coding_agent": _safe_string(
            handoff.get("coding_agent_next_action"), FALLBACK_ACTION
        ),
        "owner": _safe_string(handoff.get("owner_next_action"), FALLBACK_ACTION),
        "dashboard": _safe_string(handoff.get("next_action"), FALLBACK_ACTION),
    }
    routing = handoff.get("routing") if isinstance(handoff.get("routing"), dict) else {}
    if failed_stages:
        may_continue = False
    elif blocked_reason == "grade_c":
        may_continue = True
    else:
        may_continue = overall != "blocked"
    packet = {
        "schema_version": SCHEMA_VERSION,
        "kind": PACKET_KIND,
        "audience": canonical,
        "audience_requested": requested,
        "trusted_receipt": True,
        "overall_status": overall,
        "blocked_reason": blocked_reason,
        "failed_stages": list(failed_stages),
        "grade": grade,
        "who_acts_first": who,
        "safe_for_autonomous_ops": bool(handoff.get("safe_for_autonomous_ops"))
        and overall != "blocked",
        "may_continue_offline_code": may_continue,
        "must_wait_for_owner": who == "owner",
        "next_action": next_by_audience[canonical],
        "andrea_next_action": next_by_audience["andrea"],
        "coding_agent_next_action": next_by_audience["coding_agent"],
        "owner_next_action": next_by_audience["owner"],
        "holds": _safe_string_list(handoff.get("holds"), DEFAULT_HOLDS),
        "routing": {
            "andrea": _safe_string(routing.get("andrea"), DEFAULT_ROUTING["andrea"]),
            "coding_agent": _safe_string(
                routing.get("coding_agent"), DEFAULT_ROUTING["coding_agent"]
            ),
            "owner": _safe_string(routing.get("owner"), DEFAULT_ROUTING["owner"]),
        },
        "actions": _safe_actions(handoff.get("actions")),
        "commands": {
            "rerun": RERUN_COMMAND,
            "verify": VERIFY_COMMAND,
            "consume": CONSUME_COMMAND,
        },
        "receipt_fingerprint": _safe_string(
            receipt.get("receipt_fingerprint"), "", limit=64
        ),
        "reason": "ok",
    }
    return packet


def consume_receipt(path: Path, audience: str) -> tuple[int, dict[str, Any]]:
    ok, reason, receipt = load_receipt(path)
    if not ok:
        return 1, audience_packet({}, audience, trusted=False, reason=reason)
    return 0, audience_packet(receipt, audience, trusted=True, reason="ok")


def format_summary(receipt: dict[str, Any]) -> str:
    handoff = _handoff_map(receipt.get("handoff"))
    failed = receipt.get("failed_stages")
    if not isinstance(failed, list):
        failed = handoff.get("failed_stages") if isinstance(handoff.get("failed_stages"), list) else []
    failed_text = ",".join(_safe_string(item, limit=40) for item in failed if item) or "-"
    return "\n".join(
        [
            f"Receipt schema: {receipt.get('schema_version')}",
            "Receipt verify: valid",
            f"overall_status={receipt.get('overall_status')}",
            f"blocked_reason={receipt.get('blocked_reason') or handoff.get('blocked_reason') or '-'}",
            f"who_acts_first={handoff.get('who_acts_first')}",
            f"failed_stages={failed_text}",
            "safe_for_autonomous_ops="
            + ("true" if handoff.get("safe_for_autonomous_ops") else "false"),
            f"Verify: {VERIFY_COMMAND}",
            f"Consume: {CONSUME_COMMAND}",
        ]
    )


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write or consume a safe offline doctor receipt"
    )
    parser.add_argument("--readiness-json", type=Path)
    parser.add_argument("--security-status")
    parser.add_argument("--reliability-status")
    parser.add_argument("--openclaw-status")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path, help="Validate a receipt and print status JSON")
    parser.add_argument(
        "--consume",
        type=Path,
        help="Print an allowlisted audience packet from a receipt",
    )
    parser.add_argument(
        "--audience",
        default="dashboard",
        help="andrea, coding_agent (bob/codex/grok/claude), owner, or dashboard",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Print operator key=value lines after a trusted verify",
    )
    args = parser.parse_args()

    consumer_flags = [flag for flag in (args.verify, args.consume, args.summary) if flag]
    if len(consumer_flags) > 1:
        print("Use only one of --verify, --consume, or --summary", file=sys.stderr)
        return 2
    if args.verify:
        ok, reason, receipt = load_receipt(args.verify)
        if not ok:
            _print_json(
                {
                    "ok": False,
                    "overall_status": "blocked",
                    "blocked_reason": "invalid_receipt",
                    "reason": reason,
                }
            )
            return 1
        _print_json(
            {
                "ok": True,
                "overall_status": receipt["overall_status"],
                "blocked_reason": receipt.get("blocked_reason")
                or receipt.get("handoff", {}).get("blocked_reason")
                or blocked_reason_for(
                    failed_stages=failed_gate_stages(
                        str(receipt["stages"]["security"]["status"]),
                        str(receipt["stages"]["reliability"]["status"]),
                    ),
                    grade=str(receipt["stages"]["readiness"]["grade"]),
                    exit_code=int(receipt["exit_code"]),
                ),
                "failed_stages": receipt.get("failed_stages")
                or failed_gate_stages(
                    str(receipt["stages"]["security"]["status"]),
                    str(receipt["stages"]["reliability"]["status"]),
                ),
                "who_acts_first": receipt.get("handoff", {}).get("who_acts_first"),
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "reason": "ok",
            }
        )
        return 0
    if args.consume:
        rc, packet = consume_receipt(args.consume, args.audience)
        _print_json(packet)
        return rc
    if args.summary:
        ok, reason, receipt = load_receipt(args.summary)
        if not ok:
            print(f"Receipt verify: invalid ({reason})", file=os.sys.stderr)
            print("overall_status=blocked")
            print("blocked_reason=invalid_receipt")
            print("who_acts_first=owner")
            print("safe_for_autonomous_ops=false")
            return 1
        print(format_summary(receipt))
        return 0

    required = (
        args.security_status,
        args.reliability_status,
        args.openclaw_status,
        args.exit_code,
        args.output,
    )
    if any(value is None for value in required):
        parser.error(
            "write mode requires --security-status, --reliability-status, "
            "--openclaw-status, --exit-code, and --output"
        )
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
