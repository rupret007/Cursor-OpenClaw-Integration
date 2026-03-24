"""
Bounded collaboration runtime: advisory multi-role rounds on verify_fail / trust_gate.

Uses OpenClaw repair lanes (triage + challenger_patch) for structured JSON when
`ANDREA_SYNC_COLLAB_RUNTIME_ENABLED` is on; otherwise only deterministic metadata applies.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .collaboration_schema import (
    ArbitrationDecision,
    RepairRecommendation,
)
from .activation_policy import operator_action_promotion_confirmed
from .collaboration_promotion import bounded_action_promotion_allows
from .observability import metric_log
from .repair_adapters import REPO_ROOT, run_role_json
from .repair_prompts import REPAIR_JSON_MARKER

COLLABORATION_RUNTIME_VERSION = 2

_STRATEGIST_SCHEMA: Dict[str, Any] = {
    "analysis": "",
    "recommended_strategy": "switch_lane|retry_same_lane|ask_user|invoke_repair_cycle|none",
    "target_lane": "",
    "rationale": "",
    "confidence": 0.0,
}

_CRITIC_SCHEMA: Dict[str, Any] = {
    "accept_strategist": True,
    "issues": [],
    "recommended_override": "none|ask_user|switch_lane|retry_same_lane|invoke_repair_cycle",
    "rationale": "",
}


def collaboration_runtime_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_RUNTIME_ENABLED") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def collaboration_advisory_only() -> bool:
    """Default on: no automatic repair actions (lane switch / retry / repair cycle)."""
    v = (os.environ.get("ANDREA_SYNC_COLLAB_ADVISORY_ONLY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def collaboration_action_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_ACTION_ENABLED") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _repo_path_for_collaboration() -> Path:
    for key in ("ANDREA_CURSOR_REPO", "ANDREA_SYNC_CURSOR_REPO"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return REPO_ROOT


def _clip(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _json_contract(schema: Dict[str, Any]) -> str:
    return (
        f"Return exactly one single-line marker in this format:\n"
        f"{REPAIR_JSON_MARKER} {json.dumps(schema, ensure_ascii=False)}\n"
        "Do not wrap the marker in a code block.\n"
    )


def build_repair_strategist_prompt(
    *,
    scenario_id: str,
    trigger: str,
    verdict: str,
    outcome_summary: str,
    lane: str,
    deterministic_strategy: str,
    candidate_lanes: List[str],
    pr_url: str,
    agent_url: str,
) -> str:
    lanes = ", ".join(candidate_lanes) if candidate_lanes else "(none listed)"
    return (
        "You are Andrea's repair_strategist on the delegated task path (bounded collaboration).\n"
        "The deterministic policy already chose a default next step; you may refine it using evidence.\n"
        "Stay concise; no user-facing prose; structured output only.\n\n"
        f"Scenario: {scenario_id}\n"
        f"Trigger: {trigger}\n"
        f"Verifier verdict: {verdict}\n"
        f"Outcome summary: {_clip(outcome_summary, 1200)}\n"
        f"Current lane: {lane}\n"
        f"Allowed candidate lanes: {lanes}\n"
        f"Deterministic strategist strategy: {deterministic_strategy}\n"
        f"PR URL present: {bool(str(pr_url or '').strip())}\n"
        f"Agent URL present: {bool(str(agent_url or '').strip())}\n\n"
        "Choose recommended_strategy from the enum in the schema.\n"
        "- Prefer ask_user when proof is missing and escalation is safer than blind automation.\n"
        "- switch_lane only if another allowed lane could plausibly obtain proof.\n"
        "- retry_same_lane only when the same lane should be re-run with tighter proof instructions.\n"
        "- invoke_repair_cycle only when an operator-style incident repair pipeline may help.\n"
        "- none when the deterministic strategy should stand.\n\n"
        f"{_json_contract(_STRATEGIST_SCHEMA)}"
    )


def build_critic_prompt(
    *,
    strategist_payload: Dict[str, Any],
    scenario_id: str,
    trigger: str,
    verdict: str,
    outcome_summary: str,
    proof_requirements: str,
) -> str:
    strat_blob = json.dumps(strategist_payload, ensure_ascii=False, indent=2)[:4000]
    return (
        "You are Andrea's critic for a bounded repair-strategy proposal after delegated verification.\n"
        "Reject unsafe or overconfident strategies. Prefer human escalation when proof is insufficient.\n"
        "Structured output only.\n\n"
        f"Scenario: {scenario_id}\nTrigger: {trigger}\nVerdict: {verdict}\n"
        f"Outcome summary: {_clip(outcome_summary, 1000)}\n"
        f"Proof expectations: {_clip(proof_requirements, 600)}\n\n"
        f"Strategist JSON:\n{strat_blob}\n\n"
        f"{_json_contract(_CRITIC_SCHEMA)}"
    )


def _normalize_strategy(raw: str) -> str:
    s = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "retry": "retry_same_lane",
        "retry_same": "retry_same_lane",
        "human": "ask_user",
        "escalate": "ask_user",
        "repair_cycle": "invoke_repair_cycle",
        "incident": "invoke_repair_cycle",
        "incident_escalation": "invoke_repair_cycle",
        "stop": "none",
    }
    return aliases.get(s, s)


def _prior_role_invocations(plan_summary: Dict[str, Any]) -> int:
    c = plan_summary.get("collaboration")
    if not isinstance(c, dict):
        return 0
    try:
        return max(0, int(c.get("role_invocation_count") or 0))
    except (TypeError, ValueError):
        return 0


def _budget_max_roles() -> int:
    try:
        return max(1, int(os.environ.get("ANDREA_SYNC_COLLAB_MAX_ROLE_CALLS", "4") or 4))
    except (TypeError, ValueError):
        return 4


def advisory_live_roles_eligible(
    *,
    scenario_id: str,
    trigger: str,
    plan_summary: Dict[str, Any],
) -> bool:
    """Measured pack: repoHelpVerified + verificationSensitiveAction, verify_fail / trust_gate, role budget."""
    if not collaboration_runtime_enabled():
        return False
    sid = str(scenario_id or "").strip()
    if sid not in ("repoHelpVerified", "verificationSensitiveAction"):
        return False
    if trigger not in ("verify_fail", "trust_gate"):
        return False
    prior = _prior_role_invocations(plan_summary)
    if prior + 2 > _budget_max_roles():
        return False
    return True


def _arbiter_choose_strategy(
    *,
    deterministic_strategy: str,
    strategist_ok: bool,
    strategist_payload: Dict[str, Any],
    critic_ok: bool,
    critic_payload: Dict[str, Any],
    candidate_lanes: List[str],
    current_lane: str,
) -> Tuple[str, str, str]:
    """
    Returns (final_strategy, arbitration_note, usefulness_bucket).
    final_strategy uses arbitration_policy vocabulary:
    ask_user | switch_lane | retry_same | incident_escalation_hint
    """
    det = _normalize_strategy(deterministic_strategy)
    if det == "retry_same_lane":
        det = "retry_same"
    if det not in ("ask_user", "switch_lane", "retry_same", "incident_escalation_hint"):
        det = "ask_user"

    if not strategist_ok:
        return det, "deterministic_only_strategist_failed", "wasteful_roles_failed"

    sp = strategist_payload or {}
    cp = critic_payload or {} if critic_ok else {}

    issues = cp.get("issues") if isinstance(cp.get("issues"), list) else []
    concrete_issues = [str(x).strip() for x in issues if str(x).strip()]
    critic_rejects = critic_ok and cp.get("accept_strategist") is False and bool(concrete_issues)

    if critic_rejects:
        return (
            "ask_user",
            "critic_rejected_strategist",
            "useful_safety_escalation" if det != "ask_user" else "informational_no_shift",
        )

    override = _normalize_strategy(str(cp.get("recommended_override") or "none"))
    if critic_ok and override == "ask_user":
        return "ask_user", "critic_override_ask_user", "useful_safety_escalation" if det != "ask_user" else "informational_no_shift"

    rec = _normalize_strategy(str(sp.get("recommended_strategy") or "none"))
    conf = float(sp.get("confidence") or 0.0)
    if rec in ("none", "") or conf < 0.25:
        return det, "strategist_defer_to_deterministic", "informational_no_shift"

    cur = str(current_lane or "").strip().lower()
    alts = [ln for ln in candidate_lanes if str(ln).strip().lower() != cur]

    if rec == "ask_user":
        return "ask_user", "strategist_ask_user", "useful_strategy_shift" if det != "ask_user" else "informational_no_shift"

    if rec == "invoke_repair_cycle":
        return (
            "incident_escalation_hint",
            "strategist_invoke_repair_cycle",
            "useful_strategy_shift" if det != "incident_escalation_hint" else "informational_no_shift",
        )

    if rec == "switch_lane":
        if not alts:
            return det, "switch_lane_unavailable_no_alternate", "wasteful_no_alternate_lane"
        target = str(sp.get("target_lane") or "").strip()
        if target and target.lower() not in {str(x).lower() for x in candidate_lanes}:
            return det, "strategist_target_lane_not_allowed", "wasteful_invalid_lane"
        return (
            "switch_lane",
            "strategist_switch_lane",
            "useful_strategy_shift" if det != "switch_lane" else "informational_no_shift",
        )

    if rec == "retry_same_lane":
        return (
            "retry_same",
            "strategist_retry_same_lane",
            "useful_strategy_shift" if det != "retry_same" else "informational_no_shift",
        )

    return det, "strategist_unmapped", "informational_no_shift"


def run_collaboration_round(
    *,
    task_id: str,
    plan_id: str,
    collab_id: str,
    scenario_id: str,
    trigger: str,
    verdict: str,
    outcome_summary: str,
    lane: str,
    deterministic_strategy: str,
    candidate_lanes: List[str],
    pr_url: str,
    agent_url: str,
    proof_requirements: str,
    repair: RepairRecommendation,
    arbitration: ArbitrationDecision,
) -> Optional[Dict[str, Any]]:
    """
    Run repair_strategist (triage lane) + critic (challenger lane), then deterministic arbiter.
    Returns merge bundle or None if skipped/failed entirely before persistence merge.
    """
    repo = _repo_path_for_collaboration()
    incident_id = f"collab-{collab_id}"

    t0 = time.monotonic()
    strat_prompt = build_repair_strategist_prompt(
        scenario_id=scenario_id,
        trigger=trigger,
        verdict=verdict,
        outcome_summary=outcome_summary,
        lane=lane,
        deterministic_strategy=deterministic_strategy,
        candidate_lanes=candidate_lanes,
        pr_url=pr_url,
        agent_url=agent_url,
    )
    strat_result = run_role_json(role="triage", prompt=strat_prompt, incident_id=incident_id, repo_path=repo)
    t1 = time.monotonic()

    strat_payload = strat_result.get("payload") if isinstance(strat_result.get("payload"), dict) else {}
    crit_payload: Dict[str, Any] = {}
    crit_result: Dict[str, Any] = {}
    t2 = t1
    if strat_result.get("ok"):
        crit_prompt = build_critic_prompt(
            strategist_payload=strat_payload,
            scenario_id=scenario_id,
            trigger=trigger,
            verdict=verdict,
            outcome_summary=outcome_summary,
            proof_requirements=proof_requirements,
        )
        crit_result = run_role_json(
            role="challenger_patch", prompt=crit_prompt, incident_id=incident_id, repo_path=repo
        )
        t2 = time.monotonic()
        crit_payload = crit_result.get("payload") if isinstance(crit_result.get("payload"), dict) else {}

    strat_ok = bool(strat_result.get("ok"))
    crit_ok = bool(crit_result.get("ok"))

    final_strategy, arb_note, usefulness = _arbiter_choose_strategy(
        deterministic_strategy=deterministic_strategy,
        strategist_ok=strat_ok,
        strategist_payload=strat_payload,
        critic_ok=crit_ok,
        critic_payload=crit_payload,
        candidate_lanes=list(candidate_lanes or []),
        current_lane=lane,
    )

    analysis = _clip(strat_payload.get("analysis") or strat_payload.get("rationale") or "", 600)
    extra_rationale = f"\n[advisory:{arb_note}] {analysis}".strip()
    new_rationale = (repair.rationale or "")[:700] + (f"\n{extra_rationale}" if analysis or arb_note else "")

    target_lane = str(strat_payload.get("target_lane") or "").strip()
    proof_plan = repair.proof_plan
    if final_strategy == "switch_lane" and target_lane:
        proof_plan = (
            f"{proof_plan}\n\nAdvisory target lane hint: `{target_lane}` (must stay within scenario contract)."
        )[:1200]

    new_repair = replace(
        repair,
        strategy=final_strategy,
        rationale=new_rationale[:1200],
        proof_plan=proof_plan[:1200],
    )
    new_arb = replace(
        arbitration,
        decision="advisory_arbitration",
        repair_strategy=final_strategy,
        trusted_to_continue=False,
    )

    role_events: List[Dict[str, Any]] = [
        {
            "task_id": task_id,
            "plan_id": plan_id,
            "collab_id": collab_id,
            "role": "repair_strategist",
            "runner_role": "triage",
            "ok": strat_ok,
            "provider": strat_result.get("provider") or "",
            "model": strat_result.get("model") or "",
            "latency_ms": int(max(0, (t1 - t0) * 1000)),
            "recommended_strategy": strat_payload.get("recommended_strategy"),
            "confidence": strat_payload.get("confidence"),
            "error": strat_result.get("error") or "",
        },
        {
            "task_id": task_id,
            "plan_id": plan_id,
            "collab_id": collab_id,
            "role": "critic",
            "runner_role": "challenger_patch",
            "ok": crit_ok,
            "provider": crit_result.get("provider") or "",
            "model": crit_result.get("model") or "",
            "latency_ms": int(max(0, (t2 - t1) * 1000)),
            "accept_strategist": crit_payload.get("accept_strategist"),
            "issues": crit_payload.get("issues") if isinstance(crit_payload.get("issues"), list) else [],
            "error": crit_result.get("error") or "",
        },
    ]

    metric_log(
        "collaboration_advisory_round",
        scenario_id=scenario_id,
        trigger=trigger,
        strategist_ok=str(strat_ok).lower(),
        critic_ok=str(crit_ok).lower(),
        final_strategy=final_strategy,
        usefulness=usefulness,
    )

    live_round = {
        "strategist": {k: strat_result.get(k) for k in ("ok", "provider", "model", "error", "payload")},
        "critic": {k: crit_result.get(k) for k in ("ok", "provider", "model", "error", "payload")},
        "arbitration_note": arb_note,
        "usefulness_status": usefulness,
        "final_strategy": final_strategy,
    }

    return {
        "repair": new_repair,
        "arbitration": new_arb,
        "role_events": role_events,
        "live_round": live_round,
        "role_invocation_delta": 2 if strat_ok or crit_ok else 0,
        "usefulness_status": usefulness,
        "target_lane_hint": target_lane,
    }


def maybe_execute_bounded_collaboration_action(
    *,
    conn: Optional[sqlite3.Connection] = None,
    scenario_id: str,
    task_id: str,
    plan_id: str,
    collab_id: str,
    final_strategy: str,
    lane: str,
    candidate_lanes: List[str],
    target_lane_hint: str,
    plan_summary: Dict[str, Any],
    outcome_summary: str,
    trigger: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Returns (summary_patch_fragment, recovery_patch_fragment, repair_dispatch_or_none).
    Only runs when action mode is enabled and scenario is repoHelpVerified.
    """
    empty: Dict[str, Any] = {}
    if collaboration_advisory_only() or not collaboration_action_enabled():
        return empty, empty, None
    if not operator_action_promotion_confirmed():
        metric_log(
            "collaboration_bounded_action_blocked",
            reason="operator_action_promotion_required",
            scenario_id=str(scenario_id or ""),
        )
        return empty, empty, None
    strategy = str(final_strategy or "").strip()
    if not bounded_action_promotion_allows(
        conn, scenario_id=str(scenario_id or ""), trigger=str(trigger or ""), strategy=strategy
    ):
        metric_log(
            "collaboration_bounded_action_blocked",
            reason="bounded_action_promotion_revision_required",
            scenario_id=str(scenario_id or ""),
        )
        return empty, empty, None
    sid = str(scenario_id or "").strip()
    if sid != "repoHelpVerified":
        return empty, empty, None

    cprev = plan_summary.get("collaboration") if isinstance(plan_summary.get("collaboration"), dict) else {}
    if bool(cprev.get("bounded_action_executed")):
        return empty, empty, None

    cur = str(lane or "").strip().lower()
    alts = [ln for ln in candidate_lanes if str(ln).strip().lower() != cur]

    summary_frag: Dict[str, Any] = {
        "collaboration": {
            "bounded_action_executed": True,
            "last_executed_action": {},
        }
    }
    recovery_frag: Dict[str, Any] = {}
    dispatch: Optional[Dict[str, Any]] = None

    if strategy == "switch_lane" and alts:
        target = str(target_lane_hint or "").strip()
        if not target or target.lower() not in {str(x).lower() for x in candidate_lanes}:
            target = alts[0]
        summary_frag["collaboration"]["last_executed_action"] = {
            "type": "switch_lane",
            "from_lane": lane,
            "to_lane": target,
        }
        summary_frag["collaboration"]["next_lane_hint"] = target
        recovery_frag["collaboration_action_executed"] = {
            "type": "switch_lane",
            "target_lane": target,
            "collab_id": collab_id,
        }
    elif strategy == "retry_same":
        summary_frag["collaboration"]["last_executed_action"] = {
            "type": "retry_same_lane",
            "lane": lane,
        }
        recovery_frag["collaboration_action_executed"] = {
            "type": "retry_same_lane",
            "lane": lane,
            "collab_id": collab_id,
        }
    elif strategy == "incident_escalation_hint":
        summary_frag["collaboration"]["last_executed_action"] = {
            "type": "invoke_repair_cycle",
        }
        recovery_frag["collaboration_action_executed"] = {
            "type": "invoke_repair_cycle",
            "collab_id": collab_id,
        }
        dispatch = {
            "kind": "invoke_repair_cycle",
            "task_id": task_id,
            "plan_id": plan_id,
            "collab_id": collab_id,
            "incident_payload": {
                "summary": _clip(
                    f"Delegated verification collaboration dispatch: {trigger} :: {outcome_summary}",
                    500,
                ),
                "error_type": "delegated_verification_failure",
                "stack_trace": _clip(f"{trigger}\n{outcome_summary}", 2000),
                "source": "collaboration_runtime",
                "source_task_id": task_id,
                "metadata": {
                    "plan_id": plan_id,
                    "collab_id": collab_id,
                    "trigger": trigger,
                },
            },
        }
    else:
        # No-op action path: clear the executed flags we pre-built
        return empty, empty, None

    metric_log(
        "collaboration_bounded_action",
        scenario_id=sid,
        action=str(summary_frag["collaboration"]["last_executed_action"].get("type") or ""),
    )
    return summary_frag, recovery_frag, dispatch
