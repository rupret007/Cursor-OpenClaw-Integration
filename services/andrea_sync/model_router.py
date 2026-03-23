"""Role-aware model selection helpers (Phase 2 / 7 blueprint)."""
from __future__ import annotations

import os
from typing import Literal

ModelRole = Literal["router", "planner", "worker", "verifier", "persona"]
CollaborationModelRole = Literal[
    "planner",
    "executor",
    "critic",
    "verifier",
    "repair_strategist",
    "persona_surface",
]


def model_for_role(role: ModelRole) -> str:
    """Resolve model label from env with sane defaults."""
    env_map = {
        "router": "ANDREA_MODEL_ROUTER",
        "planner": "ANDREA_MODEL_PLANNER",
        "worker": "ANDREA_MODEL_WORKER",
        "verifier": "ANDREA_MODEL_VERIFIER",
        "persona": "ANDREA_MODEL_PERSONA",
    }
    key = env_map.get(role, "ANDREA_MODEL_WORKER")
    default = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "gpt-4o-mini").strip()
    return (os.environ.get(key) or default).strip()


def model_for_collaboration_role(role: CollaborationModelRole) -> str:
    """Map bounded-collaboration roles onto existing env-backed model slots (slice 2 wiring)."""
    mapped: ModelRole = {
        "planner": "planner",
        "executor": "worker",
        "critic": "verifier",
        "verifier": "verifier",
        "repair_strategist": "planner",
        "persona_surface": "persona",
    }.get(role, "worker")
    return model_for_role(mapped)
