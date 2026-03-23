"""Provider-agnostic model lane adapters for incident repairs."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .repair_prompts import REPAIR_JSON_MARKER

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clip(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _normalize_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split())


def _collect_text_parts(value: Any, out: List[str]) -> None:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
        for inner in value.values():
            _collect_text_parts(inner, out)
        return
    if isinstance(value, list):
        for inner in value:
            _collect_text_parts(inner, out)


def _extract_repair_json(text: str) -> Dict[str, Any]:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(REPAIR_JSON_MARKER):
            continue
        raw = stripped[len(REPAIR_JSON_MARKER) :].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid repair JSON marker: {_clip(raw, 300)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("repair JSON marker must decode to an object")
        return payload
    raise RuntimeError("repair JSON marker missing from model response")


@dataclass(frozen=True)
class ModelRoleConfig:
    role: str
    preferred_model_family: str
    preferred_model_label: str
    agent_id: str
    thinking: str
    timeout_seconds: int


def _role_config(role: str) -> ModelRoleConfig:
    normalized = str(role or "").strip().lower()
    default_agent_id = (os.environ.get("ANDREA_OPENCLAW_AGENT_ID") or "main").strip() or "main"
    defaults = {
        "triage": ModelRoleConfig(
            role="triage",
            preferred_model_family=os.environ.get("ANDREA_REPAIR_TRIAGE_MODEL_FAMILY", "gemini").strip()
            or "gemini",
            preferred_model_label=os.environ.get(
                "ANDREA_REPAIR_TRIAGE_MODEL_LABEL", "Gemini Flash Lite"
            ).strip()
            or "Gemini Flash Lite",
            agent_id=(
                os.environ.get("ANDREA_REPAIR_TRIAGE_AGENT_ID")
                or os.environ.get("ANDREA_REPAIR_AGENT_ID")
                or default_agent_id
            ).strip()
            or default_agent_id,
            thinking=os.environ.get("ANDREA_REPAIR_TRIAGE_THINKING", "low").strip() or "low",
            timeout_seconds=max(
                60, int(os.environ.get("ANDREA_REPAIR_TRIAGE_TIMEOUT_SECONDS", "300"))
            ),
        ),
        "primary_patch": ModelRoleConfig(
            role="primary_patch",
            preferred_model_family=os.environ.get(
                "ANDREA_REPAIR_PRIMARY_MODEL_FAMILY", "openai"
            ).strip()
            or "openai",
            preferred_model_label=os.environ.get(
                "ANDREA_REPAIR_PRIMARY_MODEL_LABEL", "GPT 5.4 mini"
            ).strip()
            or "GPT 5.4 mini",
            agent_id=(
                os.environ.get("ANDREA_REPAIR_PRIMARY_AGENT_ID")
                or os.environ.get("ANDREA_REPAIR_AGENT_ID")
                or default_agent_id
            ).strip()
            or default_agent_id,
            thinking=os.environ.get("ANDREA_REPAIR_PRIMARY_THINKING", "medium").strip()
            or "medium",
            timeout_seconds=max(
                60, int(os.environ.get("ANDREA_REPAIR_PRIMARY_TIMEOUT_SECONDS", "480"))
            ),
        ),
        "challenger_patch": ModelRoleConfig(
            role="challenger_patch",
            preferred_model_family=os.environ.get(
                "ANDREA_REPAIR_CHALLENGER_MODEL_FAMILY", "minimax"
            ).strip()
            or "minimax",
            preferred_model_label=os.environ.get(
                "ANDREA_REPAIR_CHALLENGER_MODEL_LABEL", "MiniMax M2.7"
            ).strip()
            or "MiniMax M2.7",
            agent_id=(
                os.environ.get("ANDREA_REPAIR_CHALLENGER_AGENT_ID")
                or os.environ.get("ANDREA_REPAIR_AGENT_ID")
                or default_agent_id
            ).strip()
            or default_agent_id,
            thinking=os.environ.get("ANDREA_REPAIR_CHALLENGER_THINKING", "medium").strip()
            or "medium",
            timeout_seconds=max(
                60, int(os.environ.get("ANDREA_REPAIR_CHALLENGER_TIMEOUT_SECONDS", "480"))
            ),
        ),
        "deep_debug": ModelRoleConfig(
            role="deep_debug",
            preferred_model_family=os.environ.get(
                "ANDREA_REPAIR_DEEP_MODEL_FAMILY", "openai"
            ).strip()
            or "openai",
            preferred_model_label=os.environ.get(
                "ANDREA_REPAIR_DEEP_MODEL_LABEL", "GPT 5.4"
            ).strip()
            or "GPT 5.4",
            agent_id=(
                os.environ.get("ANDREA_REPAIR_DEEP_AGENT_ID")
                or os.environ.get("ANDREA_REPAIR_AGENT_ID")
                or default_agent_id
            ).strip()
            or default_agent_id,
            thinking=os.environ.get("ANDREA_REPAIR_DEEP_THINKING", "high").strip()
            or "high",
            timeout_seconds=max(
                60, int(os.environ.get("ANDREA_REPAIR_DEEP_TIMEOUT_SECONDS", "900"))
            ),
        ),
    }
    if normalized not in defaults:
        raise ValueError(f"unknown repair role: {role}")
    return defaults[normalized]


def _normalize_model_token(value: Any) -> str:
    return "".join(ch for ch in _normalize_whitespace(value).casefold() if ch.isalnum())


def _routing_verdict(
    *,
    role_config: ModelRoleConfig,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    requested_family = _normalize_model_token(role_config.preferred_model_family)
    requested_label = _normalize_model_token(role_config.preferred_model_label)
    actual_provider = _normalize_model_token(provider)
    actual_model = _normalize_model_token(model)
    provider_match = not requested_family or not actual_provider or requested_family == actual_provider
    model_match = not requested_label or not actual_model or requested_label in actual_model
    matched = provider_match and model_match
    if matched:
        return {
            "matched": True,
            "status": "matched",
            "reason": "provider_and_model_match_requested_route",
        }
    reasons: List[str] = []
    if not provider_match:
        reasons.append("provider_mismatch")
    if not model_match:
        reasons.append("model_mismatch")
    return {
        "matched": False,
        "status": "mismatch",
        "reason": ",".join(reasons) or "route_mismatch",
        "requested_family": role_config.preferred_model_family,
        "requested_label": role_config.preferred_model_label,
        "actual_provider": provider,
        "actual_model": model,
    }


def _build_message(*, role_config: ModelRoleConfig, incident_id: str, prompt: str) -> str:
    return (
        "You are running inside Andrea's incident repair orchestration runtime.\n"
        f"Role: {role_config.role}\n"
        f"Incident id: {incident_id}\n"
        f"Agent profile: {role_config.agent_id}\n"
        f"Preferred model family: {role_config.preferred_model_family}\n"
        f"Preferred model label: {role_config.preferred_model_label}\n"
        "Stay within the requested role and return the requested structured marker exactly once.\n\n"
        f"{prompt.strip()}\n"
    )


def run_role_json(
    *,
    role: str,
    prompt: str,
    incident_id: str,
    repo_path: Path,
) -> Dict[str, Any]:
    role_config = _role_config(role)
    strict_match = str(os.environ.get("ANDREA_REPAIR_STRICT_MODEL_MATCH") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        role_config.agent_id,
        "--message",
        _build_message(role_config=role_config, incident_id=incident_id, prompt=prompt),
        "--json",
        "--timeout",
        str(role_config.timeout_seconds),
        "--thinking",
        role_config.thinking,
        "--session-id",
        f"andrea-repair-{role_config.preferred_model_family}-{uuid.uuid4().hex[:12]}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path or REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=max(15, role_config.timeout_seconds + 5),
    )
    stdout = str(proc.stdout or "").strip()
    stderr = str(proc.stderr or "").strip()
    if not stdout:
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "error": _clip(stderr or "empty OpenClaw response", 500),
        }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "error": f"invalid OpenClaw JSON: {_clip(stdout, 300)}",
            "exception": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "error": "OpenClaw returned a non-object payload",
        }
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    payloads = result.get("payloads") if isinstance(result.get("payloads"), list) else []
    texts: List[str] = []
    _collect_text_parts(payloads, texts)
    combined_text = "\n".join(texts).strip()
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    provider = _normalize_whitespace(agent_meta.get("provider") or "")
    model = _normalize_whitespace(agent_meta.get("model") or "")
    routing = _routing_verdict(role_config=role_config, provider=provider, model=model)
    if proc.returncode != 0:
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "agent_id": role_config.agent_id,
            "provider": provider,
            "model": model,
            "routing": routing,
            "raw_text": _clip(combined_text, 4000),
            "error": _clip(payload.get("error") or stderr or "openclaw agent failed", 500),
        }
    try:
        repair_json = _extract_repair_json(combined_text)
    except RuntimeError as exc:
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "agent_id": role_config.agent_id,
            "provider": provider,
            "model": model,
            "routing": routing,
            "raw_text": _clip(combined_text, 4000),
            "error": str(exc),
        }
    if strict_match and not routing.get("matched"):
        return {
            "ok": False,
            "role": role_config.role,
            "requested_family": role_config.preferred_model_family,
            "requested_label": role_config.preferred_model_label,
            "agent_id": role_config.agent_id,
            "provider": provider,
            "model": model,
            "routing": routing,
            "raw_text": _clip(combined_text, 4000),
            "error": f"repair lane routing mismatch: {routing.get('reason')}",
        }
    return {
        "ok": True,
        "role": role_config.role,
        "requested_family": role_config.preferred_model_family,
        "requested_label": role_config.preferred_model_label,
        "agent_id": role_config.agent_id,
        "provider": provider,
        "model": model,
        "routing": routing,
        "payload": repair_json,
        "raw_text": _clip(combined_text, 4000),
    }
