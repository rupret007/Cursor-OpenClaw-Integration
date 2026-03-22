"""Minimal Alexa Custom Skill request/response mapping."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Optional, Tuple

MAX_SPOKEN_CHARS = 320


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def voice_safe_text(text: str, *, default: str = "Okay.", limit: int = MAX_SPOKEN_CHARS) -> str:
    clean = _normalize_whitespace(text)
    if not clean:
        return default
    clean = re.sub(r"https?://\S+", "", clean)
    clean = re.sub(r"\bPR\b", "pull request", clean, flags=re.I)
    clean = re.sub(r"[`*_#>-]+", " ", clean)
    clean = _normalize_whitespace(clean)
    if len(clean) <= limit:
        return clean
    cut = clean[: max(0, limit - 3)].rstrip()
    return cut + "..."


def build_ack_response(
    utterance: str,
    *,
    delegated: bool,
    telegram_summary_expected: bool = True,
) -> Dict[str, Any]:
    if delegated:
        if telegram_summary_expected:
            speech = (
                "I started working on that. I will keep the voice reply short and send one summary to Telegram when it finishes."
            )
        else:
            speech = "I started working on that. I will keep the voice reply short while the rest continues in the background."
    else:
        speech = voice_safe_text(utterance, default="Okay.")
    return _response(speech, session_should_end=True)


def parse_alexa_body(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (command_for_bus_or_none, alexa_response_envelope).
    If command is None, alexa_response is standalone (e.g. LaunchRequest welcome).
    """
    req = body.get("request") if isinstance(body.get("request"), dict) else {}
    rtype = req.get("type")
    session = body.get("session") if isinstance(body.get("session"), dict) else {}
    session_id = session.get("sessionId") or str(uuid.uuid4())
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    system = context.get("System") if isinstance(context.get("System"), dict) else {}
    device = system.get("device") if isinstance(system.get("device"), dict) else {}
    user = system.get("user") if isinstance(system.get("user"), dict) else {}
    locale = req.get("locale") or ""

    if rtype == "LaunchRequest":
        return None, _response(
            "AndreaBot is here. Say, ask AndreaBot to help with something.",
            session_should_end=False,
        )

    if rtype == "SessionEndedRequest":
        return None, _response("Goodbye.", session_should_end=True)

    if rtype == "IntentRequest":
        intent = req.get("intent") if isinstance(req.get("intent"), dict) else {}
        name = intent.get("name")
        slots = intent.get("slots") if isinstance(intent.get("slots"), dict) else {}
        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return None, _response("Okay.", session_should_end=True)
        utterance = ""
        if "utterance" in slots and isinstance(slots["utterance"], dict):
            utterance = (slots["utterance"].get("value") or "").strip()
        if not utterance and name == "AndreaCaptureIntent":
            for _k, v in slots.items():
                if isinstance(v, dict) and v.get("value"):
                    utterance = str(v["value"]).strip()
                    break
        if not utterance:
            return None, _response(
                "I did not catch that. Try: ask AndreaBot to note buy milk.",
                session_should_end=False,
            )
        req_id = req.get("requestId") or session_id
        cmd = {
            "command_type": "AlexaUtterance",
            "channel": "alexa",
            "external_id": str(req_id),
            "payload": {
                "utterance": utterance,
                "text": utterance,
                "routing_text": utterance,
                "session_id": session_id,
                "request_id": str(req.get("requestId") or ""),
                "intent_name": str(name or ""),
                "locale": str(locale or ""),
                "user_id": str(user.get("userId") or ""),
                "device_id": str(device.get("deviceId") or ""),
            },
        }
        rep = _response(
            "Okay. Let me work on that.",
            session_should_end=True,
        )
        return cmd, rep

    return None, _response("Unsupported request type.", session_should_end=True)


def _response(speech: str, *, session_should_end: bool) -> Dict[str, Any]:
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": voice_safe_text(speech)},
            "shouldEndSession": session_should_end,
        },
    }


def build_response_json(response: Dict[str, Any]) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8")
