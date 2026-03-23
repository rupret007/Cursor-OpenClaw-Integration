"""Contracts for bounded multi-model collaboration and repair (v1 metadata-first)."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


def new_collaboration_id() -> str:
    return f"col_{uuid.uuid4().hex[:16]}"


@dataclass
class CollaborationRequest:
    collab_id: str
    goal_id: str
    plan_id: str
    step_id: str
    scenario_id: str
    trigger: str
    pattern: str
    required_proof_class: str
    candidate_lanes: List[str]
    budget_json: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RoleAssignment:
    role: str
    preferred_lane: str
    preferred_model_family: str
    preferred_model_label: str
    acceptance_criteria: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelContribution:
    contribution_id: str
    collab_id: str
    role: str
    provider: str
    model: str
    summary: str
    confidence: float
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CritiqueResult:
    collab_id: str
    accepted: bool
    issues: List[str]
    force_replan: bool
    requires_stronger_verifier: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RepairRecommendation:
    collab_id: str
    strategy: str
    rationale: str
    safe_scope: str
    proof_plan: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArbitrationDecision:
    collab_id: str
    decision: str
    chosen_contribution_id: str
    repair_strategy: str
    trusted_to_continue: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrustedOutcomeSummary:
    plan_id: str
    step_id: str
    verified: bool
    repair_applied: bool
    proof_summary: str
    remaining_risks: List[str]
    next_safe_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def collaboration_event_payload(
    *,
    task_id: str,
    plan_id: str,
    step_id: str,
    scenario_id: str,
    request: CollaborationRequest,
    repair: RepairRecommendation,
    arbitration: ArbitrationDecision,
    role_assignments: List[RoleAssignment],
    contribution: ModelContribution | None = None,
) -> Dict[str, Any]:
    """Payload for COLLABORATION_RECORDED (lean, operator-safe)."""
    out: Dict[str, Any] = {
        "task_id": task_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "scenario_id": scenario_id,
        "collab_id": request.collab_id,
        "trigger": request.trigger,
        "pattern": request.pattern,
        "repair_strategy": repair.strategy,
        "repair_rationale": repair.rationale[:800],
        "proof_plan": repair.proof_plan[:800],
        "arbitration_decision": arbitration.decision,
        "trusted_to_continue": arbitration.trusted_to_continue,
        "roles": [r.to_dict() for r in role_assignments],
    }
    if contribution:
        out["contribution"] = contribution.to_dict()
    return out
