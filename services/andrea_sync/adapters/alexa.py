"""Minimal Alexa Custom Skill request/response mapping."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Optional, Tuple


_KEEP_TALKING_RE = re.compile(
    r"^\s*(?:ok(?:ay)?\s*[,;:]?\s*)?(?:can\s+i\s+)?still\s+talk\s+to\s+andrea\s*\??\s*$",
    re.IGNORECASE,
)


def parse_alexa_body(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (command_for_bus_or_none, alexa_response_envelope).
    If command is None, alexa_response is standalone (e.g. LaunchRequest welcome).
    """
    req = body.get("request") or {}
    rtype = req.get("type")
    session = body.get("session") or {}
    session_id = session.get("sessionId") or str(uuid.uuid4())

    if rtype == "LaunchRequest":
        return None, _response(
            "Andrea sync is online. What should I capture or delegate?",
            session_should_end=False,
        )

    if rtype == "SessionEndedRequest":
        return None, _response("Goodbye.", session_should_end=True)

    if rtype == "IntentRequest":
        intent = req.get("intent") or {}
        name = intent.get("name")
        slots = intent.get("slots") or {}
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
                "I did not catch that. Try: ask Andrea to note buy milk.",
                session_should_end=False,
            )
        if _KEEP_TALKING_RE.match(utterance):
            return None, _response(
                "Yes, you can still talk to Andrea. What should I capture or delegate?",
                session_should_end=False,
            )
        req_id = req.get("requestId") or session_id
        cmd = {
            "command_type": "AlexaUtterance",
            "channel": "alexa",
            "external_id": str(req_id),
            "payload": {"utterance": utterance, "session_id": session_id},
        }
        rep = _response(
            f"Captured. Task queued. You said: {utterance[:120]}",
            session_should_end=True,
        )
        return cmd, rep

    return None, _response("Unsupported request type.", session_should_end=True)


def _response(speech: str, *, session_should_end: bool) -> Dict[str, Any]:
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech},
            "shouldEndSession": session_should_end,
        },
    }


def build_response_json(response: Dict[str, Any]) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8")
