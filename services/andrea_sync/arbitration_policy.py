"""Bounded collaboration triggers and deterministic repair strategist (v1)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .collaboration_schema import (
    ArbitrationDecision,
    CollaborationRequest,
    ModelContribution,
    RepairRecommendation,
    RoleAssignment,
    new_collaboration_id,
)
from .scenario_runtime import lane_allowed_for_scenario, normalize_execution_lane_for_scenario

# First collaboration-enabled pack: delegated repo + proof-sensitive scenarios.
COLLABORATION_ENABLED_SCENARIOS = frozenset(
    {
        "repoHelpVerified",
        "verificationSensitiveAction",
        "multiStepTroubleshoot",
    }
)


def collaboration_layer_enabled() -> bool:
    raw = (os.environ.get("ANDREA_SYNC_COLLABORATION_LAYER") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def collaboration_budget() -> Dict[str, Any]:
    return {
        "max_rounds_per_step": max(
            1, int(os.environ.get("ANDREA_SYNC_COLLAB_MAX_ROUNDS", "2") or 2)
        ),
        "max_role_invocations": max(
            1, int(os.environ.get("ANDREA_SYNC_COLLAB_MAX_ROLE_CALLS", "4") or 4)
        ),
    }


def _prior_collab_rounds(plan_summary: Dict[str, Any]) -> int:
    c = plan_summary.get("collaboration")
    if not isinstance(c, dict):
        return 0
    try:
        return max(0, int(c.get("rounds") or 0))
    except (TypeError, ValueError):
        return 0


def should_attach_collaboration(
    *,
    scenario_id: str,
    contract: Any,
    trigger: str,
    plan_summary: Dict[str, Any],
) -> bool:
    return not bool(
        explain_collaboration_attachment_blockers(
            scenario_id=scenario_id,
            contract=contract,
            trigger=trigger,
            plan_summary=plan_summary,
        )
    )


def explain_collaboration_attachment_blockers(
    *,
    scenario_id: str,
    contract: Any,
    trigger: str,
    plan_summary: Dict[str, Any],
) -> List[str]:
    """Stable reason codes when collaboration metadata is not attached."""
    reasons: List[str] = []
    if not collaboration_layer_enabled():
        reasons.append("collaboration_layer_disabled")
        return reasons
    sid = str(scenario_id or "").strip()
    if sid not in COLLABORATION_ENABLED_SCENARIOS:
        reasons.append("scenario_not_collaboration_enabled")
    if contract is None:
        reasons.append("missing_contract")
    rounds = _prior_collab_rounds(plan_summary)
    max_rounds = int(collaboration_budget().get("max_rounds_per_step") or 2)
    if rounds >= max_rounds:
        reasons.append("collaboration_round_budget_exhausted")
    if trigger not in ("verify_fail", "trust_gate", "verify_weak"):
        reasons.append("trigger_not_collaboration_eligible")
    return reasons


def _candidate_lanes_for_contract(contract: Any, current_lane: str) -> List[str]:
    raw = list(getattr(contract, "allowed_lanes", ()) or ())
    out: List[str] = []
    for x in raw:
        n = normalize_execution_lane_for_scenario(str(x))
        if n and n not in out:
            out.append(n)
    cur = normalize_execution_lane_for_scenario(current_lane)
    if cur and cur not in out and lane_allowed_for_scenario(contract, current_lane):
        out.insert(0, cur)
    return out


def build_collaboration_bundle(
    *,
    task_id: str,
    goal_id: str,
    plan_id: str,
    step_id: str,
    scenario_id: str,
    contract: Any,
    trigger: str,
    verdict: str,
    verification_method: str,
    summary: str,
    lane: str,
    plan_summary: Dict[str, Any],
    pr_url: str = "",
    agent_url: str = "",
) -> Optional[Tuple[CollaborationRequest, RepairRecommendation, ArbitrationDecision, List[RoleAssignment], ModelContribution]]:
    """
    Deterministic repair strategist baseline; live OpenClaw roles may augment this when
    `ANDREA_SYNC_COLLAB_RUNTIME_ENABLED` is on (see `collaboration_runtime`).
    Returns artifacts to persist and optionally emit COLLABORATION_RECORDED.
    """
    if not should_attach_collaboration(
        scenario_id=scenario_id, contract=contract, trigger=trigger, plan_summary=plan_summary
    ):
        return None

    budget = collaboration_budget()
    rounds = _prior_collab_rounds(plan_summary)
    collab_id = new_collaboration_id()
    proof_class = str(getattr(contract, "proof_class", "") or getattr(contract, "verification_class", "") or "")

    candidate_lanes = _candidate_lanes_for_contract(contract, lane)
    cur_norm = normalize_execution_lane_for_scenario(lane)

    # Role assignments: repair_strategist (deterministic) + optional verifier emphasis
    roles: List[RoleAssignment] = [
        RoleAssignment(
            role="repair_strategist",
            preferred_lane=cur_norm or "openclaw_hybrid",
            preferred_model_family="",
            preferred_model_label="",
            acceptance_criteria=[
                "bounded_repair",
                "no_approval_scope_expansion",
                "reverify_after_change",
            ],
        ),
        RoleAssignment(
            role="verifier",
            preferred_lane=cur_norm or "openclaw_hybrid",
            preferred_model_family="",
            preferred_model_label="",
            acceptance_criteria=["proof_matches_scenario_contract"],
        ),
    ]

    req = CollaborationRequest(
        collab_id=collab_id,
        goal_id=str(goal_id or ""),
        plan_id=plan_id,
        step_id=step_id,
        scenario_id=str(scenario_id or ""),
        trigger=trigger,
        pattern="repair",
        required_proof_class=proof_class,
        candidate_lanes=candidate_lanes,
        budget_json=dict(budget),
    )

    has_pr = bool(str(pr_url or "").strip())
    strategy = "ask_user"
    rationale = str(summary or "")[:500]
    safe_scope = "same_goal_same_plan_no_new_side_effects"
    proof_plan = "Confirm proof artifacts (e.g. PR link) or explicitly approve a weaker outcome where policy allows."

    if trigger == "trust_gate":
        strategy = "ask_user"
        proof_plan = (
            "This job is verification-sensitive: I need explicit confirmation or stronger proof "
            "(for example a PR link) before a trusted completion receipt."
        )
    elif str(scenario_id or "") == "repoHelpVerified" and not has_pr and rounds == 0:
        # Prefer alternate delegated lane once if another lane may yield artifacts.
        alt = [ln for ln in candidate_lanes if ln != cur_norm]
        if alt:
            strategy = "switch_lane"
            proof_plan = (
                f"First verification pass lacked a PR link; try the other allowed lane (`{alt[0]}`) "
                "on a follow-up run if you want automated repo proof."
            )
            rationale = (
                "Repo verification expects a PR URL when possible; "
                "switching lane may help capture artifacts on a fresh delegated run."
            )
        else:
            strategy = "retry_same"
            proof_plan = "Re-run delegated execution after ensuring the agent publishes a PR or shareable artifact URL."
            rationale = "No alternate lane available; retry the same path with clearer proof requirements."
    elif rounds >= 1:
        strategy = "incident_escalation_hint"
        proof_plan = (
            "Collaboration budget for this step is exhausted; use operator repair flow (RunIncidentRepair) "
            "or continue manually with the captured agent/PR links."
        )
        rationale = "Bounded repair rounds exhausted; stop automatic retries."

    repair = RepairRecommendation(
        collab_id=collab_id,
        strategy=strategy,
        rationale=rationale,
        safe_scope=safe_scope,
        proof_plan=proof_plan,
    )

    arb = ArbitrationDecision(
        collab_id=collab_id,
        decision="accept_repair_plan",
        chosen_contribution_id="",
        repair_strategy=strategy,
        trusted_to_continue=False,
    )

    contrib = ModelContribution(
        contribution_id=f"ctr_{collab_id[-12:]}",
        collab_id=collab_id,
        role="repair_strategist",
        provider="andrea_sync",
        model="deterministic_v1",
        summary=f"strategy={strategy}; rounds={rounds + 1}",
        confidence=0.75 if strategy != "ask_user" else 0.55,
        artifacts={
            "verdict": verdict,
            "verification_method": verification_method,
            "has_pr_url": has_pr,
            "has_agent_url": bool(str(agent_url or "").strip()),
        },
    )

    return req, repair, arb, roles, contrib


def collaboration_summary_patch(
    prev: Dict[str, Any],
    *,
    request: CollaborationRequest,
    repair: RepairRecommendation,
    arbitration: ArbitrationDecision,
    role_invocation_delta: int = 0,
    usefulness_status: str = "",
    advisory_source: str = "",
    activation_mode: str = "",
    canonical_usefulness: str = "",
    activation_policy_version: str = "",
    activation_reason_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Keys to merge into execution_plans.summary_json (shallow update via store)."""
    prior = prev.get("collaboration") if isinstance(prev.get("collaboration"), dict) else {}
    rounds = int(prior.get("rounds") or 0) + 1
    strategies = list(prior.get("strategies") or [])
    if isinstance(strategies, list) and repair.strategy not in strategies:
        strategies.append(repair.strategy)
    try:
        prior_roles = max(0, int(prior.get("role_invocation_count") or 0))
    except (TypeError, ValueError):
        prior_roles = 0
    try:
        delta = max(0, int(role_invocation_delta or 0))
    except (TypeError, ValueError):
        delta = 0
    collab: Dict[str, Any] = {
        "rounds": rounds,
        "last_collab_id": request.collab_id,
        "last_trigger": request.trigger,
        "last_strategy": repair.strategy,
        "strategies": strategies[-8:],
        "repair_state": (
            "suggested" if repair.strategy != "incident_escalation_hint" else "escalation_hint"
        ),
        "arbitration_state": arbitration.decision,
        "role_invocation_count": prior_roles + delta,
    }
    if usefulness_status:
        collab["usefulness_status"] = str(usefulness_status)[:120]
    if advisory_source:
        collab["advisory_source"] = str(advisory_source)[:80]
    if activation_mode:
        collab["last_activation_mode"] = str(activation_mode)[:40]
    if canonical_usefulness:
        collab["last_canonical_usefulness"] = str(canonical_usefulness)[:40]
    if activation_policy_version:
        collab["activation_policy_version"] = str(activation_policy_version)[:40]
    if activation_reason_codes:
        collab["last_activation_reason_codes"] = [str(x)[:80] for x in activation_reason_codes[:8]]
    return {"collaboration": collab}
