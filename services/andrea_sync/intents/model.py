"""Typed intent envelopes used to protect direct assistant lanes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClassifiedIntent:
    type: str
    action: str
    target: str = ""
    object: str = ""
    protected_category: str = ""
    source_text: str = ""
    required_user_visible_outcome: str = ""
    risk_level: str = "low"
    time_sensitivity: str = "normal"
    coalescing_eligible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntentEnvelope:
    raw_text: str
    explicit_lane: str = ""
    intents: tuple[ClassifiedIntent, ...] = field(default_factory=tuple)
    intent_family: str = "unknown"
    action: str = ""
    object: str = ""
    protected_category: str = ""
    control_plane_flag: bool = False
    code_plane_flag: bool = False
    coalescing_eligible: bool = True
    required_user_visible_outcomes: tuple[str, ...] = field(default_factory=tuple)
    risk_level: str = "low"
    time_sensitivity: str = "normal"

    @property
    def is_multi_intent(self) -> bool:
        return len(self.intents) > 1

    @property
    def has_protected_assistant_intent(self) -> bool:
        return any(intent.protected_category for intent in self.intents)

    @property
    def has_control_plane_intent(self) -> bool:
        return any(intent.type == "control_plane" for intent in self.intents)

    @property
    def has_code_plane_intent(self) -> bool:
        return any(intent.type == "code_plane" for intent in self.intents)

    @property
    def is_explicit_continuation(self) -> bool:
        return any(intent.type == "continuation" for intent in self.intents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "explicit_lane": self.explicit_lane,
            "intents": [intent.to_dict() for intent in self.intents],
            "intent_family": self.intent_family,
            "action": self.action,
            "object": self.object,
            "protected_category": self.protected_category,
            "control_plane_flag": self.control_plane_flag,
            "code_plane_flag": self.code_plane_flag,
            "coalescing_eligible": self.coalescing_eligible,
            "required_user_visible_outcomes": list(self.required_user_visible_outcomes),
            "risk_level": self.risk_level,
            "time_sensitivity": self.time_sensitivity,
            "is_multi_intent": self.is_multi_intent,
        }
