"""Scenario contract types for the trusted assistant capability layer."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

SCENARIO_VERSION = "1"

# Support levels (product boundary)
SUPPORTED_AUTO = "supported_auto"
SUPPORTED_APPROVAL = "supported_approval"
DRAFT_ONLY = "draft_only"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ScenarioContract:
    scenario_id: str
    category: str
    support_level: str
    action_class: str
    default_plan_kind: str
    required_capabilities: tuple[str, ...]
    allowed_lanes: tuple[str, ...]
    default_risk_tier: int
    approval_mode: str  # auto | required
    verification_class: str  # repo_checks | citation | provider_receipt | human_confirm | none
    receipt_mode: str  # standard | proof_first | blocked_summary
    memory_policy: str  # default | capture_ok | read_only
    persona_mode: str
    blocks_auto_delegate: bool = False
    user_facing_label: str = ""

    @property
    def proof_class(self) -> str:
        return self.verification_class


@dataclass
class ScenarioResolution:
    scenario_id: str
    confidence: float
    support_level: str
    reason: str
    goal_id: str
    needs_plan: bool
    suggested_lane: str
    action_class: str = ""
    proof_class: str = ""
    approval_mode: str = "auto"

    def to_event_payload(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": SCENARIO_VERSION,
            "confidence": float(self.confidence),
            "support_level": self.support_level,
            "reason": self.reason,
            "goal_id": self.goal_id or "",
            "needs_plan": bool(self.needs_plan),
            "suggested_lane": self.suggested_lane or "",
            "action_class": self.action_class or "",
            "proof_class": self.proof_class or "",
            "approval_mode": self.approval_mode or "auto",
        }


@dataclass
class ScenarioReceipt:
    receipt_id: str
    scenario_id: str
    plan_id: str
    verified: bool
    proof_items: List[Dict[str, Any]]
    user_summary: str
    remaining_risks: List[str]
    next_safe_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def scenario_blob_for_job_payload(resolution: ScenarioResolution, contract: ScenarioContract) -> Dict[str, Any]:
    """Flattened scenario metadata carried on JOB_QUEUED / plan summary."""
    return {
        "scenario_id": resolution.scenario_id,
        "scenario_version": SCENARIO_VERSION,
        "support_level": contract.support_level,
        "action_class": contract.action_class,
        "proof_class": contract.proof_class,
        "receipt_state": "pending",
        "approval_mode": contract.approval_mode,
        "scenario_reason": resolution.reason[:500],
    }


def merge_scenario_into_plan_summary(
    summary: Dict[str, Any], scenario_blob: Dict[str, Any]
) -> Dict[str, Any]:
    out = dict(summary) if isinstance(summary, dict) else {}
    for k, v in scenario_blob.items():
        out[k] = v
    return out
