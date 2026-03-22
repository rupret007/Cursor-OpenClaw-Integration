#!/usr/bin/env python3
"""Reference Alexa cloud-edge forwarder for AndreaBot.

This module is designed to be usable as an AWS Lambda handler or as a
reference implementation for other public HTTPS edges. It keeps the edge
stateless and narrow:

- decode Alexa JSON requests from API Gateway/Lambda events
- optionally verify allowed Alexa application ids
- forward the raw request body to the local Andrea backend
- return a valid Alexa response envelope even on backend/auth/timeout errors

This helper does not replace Amazon signature verification. For production
or certification, perform Alexa signature/certificate validation at the
public edge before forwarding the request into Andrea.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping

DEFAULT_TIMEOUT_SECONDS = 8
MAX_PASSTHROUGH_BODY_BYTES = 262144


def build_alexa_response(
    speech: str,
    *,
    end_session: bool = True,
    reprompt: str = "",
) -> Dict[str, Any]:
    response: Dict[str, Any] = {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": str(speech or "Okay.").strip() or "Okay."},
            "shouldEndSession": end_session,
        },
    }
    if reprompt:
        response["response"]["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": str(reprompt).strip()},
        }
    return response


def build_edge_http_response(payload: Dict[str, Any], *, status_code: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json;charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def decode_event_body_bytes(event: Mapping[str, Any]) -> bytes:
    body = event.get("body", "")
    if isinstance(body, dict):
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    if body is None:
        return b""
    raw = str(body)
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw.encode("utf-8"))
    return raw.encode("utf-8")


def decode_event_body(event: Mapping[str, Any]) -> str:
    return decode_event_body_bytes(event).decode("utf-8")


def parse_alexa_request_json(event: Mapping[str, Any]) -> Dict[str, Any]:
    raw = decode_event_body_bytes(event)
    if not raw.strip():
        raise ValueError("missing request body")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Alexa request body must decode to an object")
    return payload


def extract_passthrough_headers(event: Mapping[str, Any]) -> Dict[str, str]:
    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        return {}
    out: Dict[str, str] = {}
    for key, value in headers.items():
        if key is None or value is None:
            continue
        k = str(key)
        if k.lower() in {"signature", "signaturecertchainurl"}:
            out[k] = str(value)
    return out


def extract_application_id(body: Mapping[str, Any]) -> str:
    session = body.get("session")
    if isinstance(session, Mapping):
        app = session.get("application")
        if isinstance(app, Mapping) and app.get("applicationId"):
            return str(app.get("applicationId"))
    context = body.get("context")
    if isinstance(context, Mapping):
        system = context.get("System")
        if isinstance(system, Mapping):
            app = system.get("application")
            if isinstance(app, Mapping) and app.get("applicationId"):
                return str(app.get("applicationId"))
    return ""


def verify_allowed_application_id(
    body: Mapping[str, Any],
    *,
    allowed_ids: list[str],
) -> None:
    if not allowed_ids:
        return
    application_id = extract_application_id(body)
    if not application_id:
        raise PermissionError("missing Alexa application id")
    if application_id not in allowed_ids:
        raise PermissionError("unexpected Alexa application id")


def backend_error_response(status_code: int, body_text: str = "") -> Dict[str, Any]:
    if status_code == 401:
        return build_alexa_response(
            "AndreaBot could not verify the secure edge connection. Please check the rollout settings and try again."
        )
    if status_code == 503:
        return build_alexa_response(
            "AndreaBot is temporarily unavailable right now. Please try again in a moment."
        )
    if status_code == 400:
        return build_alexa_response(
            "AndreaBot could not understand that request format. Please try again."
        )
    detail = str(body_text or "").strip()
    if detail:
        return build_alexa_response(
            "AndreaBot hit a temporary backend problem. Please try again shortly."
        )
    return build_alexa_response("AndreaBot could not complete that request right now. Please try again.")


def forward_to_andrea(
    body_bytes: bytes,
    *,
    andrea_sync_url: str,
    edge_token: str,
    passthrough_headers: Mapping[str, str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    url = andrea_sync_url.rstrip("/") + "/v1/alexa"
    if len(body_bytes) > MAX_PASSTHROUGH_BODY_BYTES:
        raise ValueError("request body too large")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {edge_token}",
    }
    headers.update({str(k): str(v) for k, v in (passthrough_headers or {}).items()})
    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=max(1, timeout_seconds)) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Andrea backend returned a non-object Alexa response")
        return payload


def handle_edge_event(
    event: Mapping[str, Any],
    *,
    andrea_sync_url: str,
    edge_token: str,
    allowed_application_ids: list[str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    allowed_ids = [value for value in (allowed_application_ids or []) if value]
    try:
        raw_body = decode_event_body_bytes(event)
        body_json = parse_alexa_request_json(event)
        verify_allowed_application_id(body_json, allowed_ids=allowed_ids)
        payload = forward_to_andrea(
            raw_body,
            andrea_sync_url=andrea_sync_url,
            edge_token=edge_token,
            passthrough_headers=extract_passthrough_headers(event),
            timeout_seconds=timeout_seconds,
        )
        return build_edge_http_response(payload)
    except PermissionError:
        return build_edge_http_response(
            build_alexa_response("AndreaBot rejected that Alexa skill identity check. Please verify the rollout config.")
        )
    except (ValueError, json.JSONDecodeError):
        return build_edge_http_response(
            build_alexa_response("AndreaBot could not read that Alexa request payload. Please try again.")
        )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return build_edge_http_response(backend_error_response(exc.code, raw))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return build_edge_http_response(
                build_alexa_response("AndreaBot timed out before the assistant backend responded. Please try again.")
            )
        return build_edge_http_response(
            build_alexa_response("AndreaBot could not reach the local assistant backend. Please try again.")
        )
    except TimeoutError:
        return build_edge_http_response(
            build_alexa_response("AndreaBot timed out before the assistant backend responded. Please try again.")
        )
    except Exception:
        return build_edge_http_response(
            build_alexa_response("AndreaBot hit an unexpected edge error. Please try again.")
        )


def handler(event: Mapping[str, Any], _context: Any) -> Dict[str, Any]:
    try:
        andrea_sync_url = os.environ["ANDREA_SYNC_URL"].rstrip("/")
        edge_token = os.environ["ANDREA_SYNC_ALEXA_EDGE_TOKEN"]
        allowed_application_ids = [
            value.strip()
            for value in os.environ.get("ALEXA_ALLOWED_APPLICATION_IDS", "").split(",")
            if value.strip()
        ]
        timeout_seconds = int(
            os.environ.get(
                "ANDREA_SYNC_ALEXA_EDGE_TIMEOUT_SECONDS",
                str(DEFAULT_TIMEOUT_SECONDS),
            )
        )
    except (KeyError, ValueError):
        return build_edge_http_response(
            build_alexa_response(
                "AndreaBot edge configuration is incomplete right now. Please check the rollout settings."
            )
        )
    return handle_edge_event(
        event,
        andrea_sync_url=andrea_sync_url,
        edge_token=edge_token,
        allowed_application_ids=allowed_application_ids,
        timeout_seconds=timeout_seconds,
    )


if __name__ == "__main__":
    demo_event = {
        "body": json.dumps(
            {
                "session": {
                    "sessionId": "demo",
                    "application": {"applicationId": "demo-app-id"},
                },
                "request": {
                    "type": "IntentRequest",
                    "requestId": "demo-request",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {"utterance": {"value": "how are you today"}},
                    },
                },
            }
        )
    }
    print(json.dumps(handler(demo_event, None), indent=2))
