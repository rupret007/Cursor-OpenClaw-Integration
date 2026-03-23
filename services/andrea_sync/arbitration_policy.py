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
    if not collaboration_layer_enabled():
        return False
    sid = str(scenario_id or "").strip()
    if sid not in COLLABORATION_ENABLED_SCENARIOS:
        return False
    if contract is None:
        return False
    rounds = _prior_collab_rounds(plan_summary)
    if rounds >= int(collaboration_budget().get("max_rounds_per_step") or 2):
        return False
    if trigger not in ("verify_fail", "trust_gate", "verify_weak"):
        return False
    return True


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
    Deterministic repair strategist: no live model calls in v1.
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
) -> Dict[str, Any]:
    """Keys to merge into execution_plans.summary_json (shallow update via store)."""
    prior = prev.get("collaboration") if isinstance(prev.get("collaboration"), dict) else {}
    rounds = int(prior.get("rounds") or 0) + 1
    strategies = list(prior.get("strategies") or [])
    if isinstance(strategies, list) and repair.strategy not in strategies:
        strategies.append(repair.strategy)
    return {
        "collaboration": {
            "rounds": rounds,
            "last_collab_id": request.collab_id,
            "last_trigger": request.trigger,
            "last_strategy": repair.strategy,
            "strategies": strategies[-8:],
            "repair_state": (
                "suggested" if repair.strategy != "incident_escalation_hint" else "escalation_hint"
            ),
            "arbitration_state": arbitration.decision,
        }
    }
