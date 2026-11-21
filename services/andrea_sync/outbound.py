"""Bind one outbound draft to the exact delivery a tool actually performed."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping
from urllib.parse import urlsplit


def canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def new_nonce() -> str:
    return secrets.token_hex(16)


def _origin(origin: Mapping[str, Any]) -> dict[str, str]:
    return {
        "channel": str(origin.get("channel") or ""),
        "chat_id": str(origin.get("chat_id") or ""),
        "message_id": str(origin.get("message_id") or ""),
    }


def approval_card(*, target: str, body: str, principal: str, origin: Mapping[str, Any],
                  expires_at: int, nonce: str) -> dict[str, Any]:
    return {
        "action": "send_message",
        "body": body,
        "expires_at": expires_at,
        "nonce": nonce,
        "origin": _origin(origin),
        "principal": principal,
        "target": target,
    }


def approval_digest(**kwargs: Any) -> str:
    card = approval_card(**kwargs)
    if type(card["expires_at"]) is not int or not card["nonce"] or not card["target"] or not card["body"]:
        raise ValueError("The outbound approval is unusable")
    return hashlib.sha256(canonical(card).encode()).hexdigest()


def bind_draft(*, target: str, body: str, principal: str, origin: Mapping[str, Any], expires_at: int,
               nonce: str, skill_key: str, label: str, created_at: float) -> dict[str, Any]:
    card = approval_card(
        target=target, body=body, principal=principal, origin=origin, expires_at=expires_at, nonce=nonce,
    )
    return {
        "action": "send_message",
        "created_at": created_at,
        "digest": hashlib.sha256(canonical(card).encode()).hexdigest(),
        "expires_at": expires_at,
        "label": label,
        "message": body,
        "nonce": nonce,
        "origin": card["origin"],
        "principal": principal,
        "skill_key": skill_key,
        "target": target,
    }


def draft_binding_ok(draft: Mapping[str, Any]) -> bool:
    try:
        expires_at = draft.get("expires_at")
        if type(expires_at) is not int:
            return False
        expected = approval_digest(
            target=str(draft.get("target") or ""),
            body=str(draft.get("message") or ""),
            principal=str(draft.get("principal") or ""),
            origin=draft.get("origin") if isinstance(draft.get("origin"), dict) else {},
            expires_at=expires_at,
            nonce=str(draft.get("nonce") or ""),
        )
    except (TypeError, ValueError):
        return False
    presented = draft.get("digest")
    if not isinstance(presented, str) or len(presented) != 64 or len(expected) != 64:
        return False
    return hmac.compare_digest(presented, expected)


def receipt_matches(draft: Mapping[str, Any], receipt: Any) -> bool:
    """A model status is not a receipt. The tool must name this exact draft."""
    if not isinstance(receipt, dict) or not draft_binding_ok(draft):
        return False
    if receipt.get("source") != "tool" or receipt.get("delivered") is not True:
        return False
    if receipt.get("tool") not in {"bluebubbles", "send_message"}:
        return False
    if receipt.get("target") != draft.get("target") or receipt.get("body") != draft.get("message"):
        return False
    presented = receipt.get("digest")
    expected = draft.get("digest")
    if not isinstance(presented, str) or not isinstance(expected, str):
        return False
    if len(presented) != 64 or len(expected) != 64:
        return False
    return hmac.compare_digest(presented, expected)


CARRIER_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
DISABLED = "Outbound delivery is disabled until a loopback carrier confirms it"
UNCONFIRMED = "The carrier did not confirm delivery"


def _carrier_target() -> tuple[int, str]:
    """The only carrier is an explicit loopback HTTP URL. Anything else stays off."""
    raw = os.environ.get("ANDREA_OUTBOUND_CARRIER", "").strip()
    parts = urlsplit(raw)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
            or parts.username or parts.password or parts.query or parts.fragment
            or not parts.path.startswith("/")):
        raise ValueError(DISABLED)
    return parts.port, parts.path


def _tool_name(draft: Mapping[str, Any]) -> str:
    tool = str(draft.get("skill_key") or "")
    if tool not in {"bluebubbles", "send_message"}:
        return "send_message"
    return tool


def _ask_carrier(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Return the carrier's confirmation. This function does not decide delivered."""
    port, path = _carrier_target()
    request = {
        "body": str(draft.get("message") or ""),
        "digest": str(draft.get("digest") or ""),
        "target": str(draft.get("target") or ""),
        "tool": _tool_name(draft),
    }
    data = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST", path, body=data, headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(4097)
        status = response.status
    except (OSError, http.client.HTTPException, TimeoutError):
        raise ValueError(UNCONFIRMED) from None
    finally:
        connection.close()
    if status != 200 or len(raw) > 4096:
        raise ValueError(UNCONFIRMED)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(UNCONFIRMED) from None
    if not isinstance(payload, dict) or payload.get("delivered") is not True:
        raise ValueError(UNCONFIRMED)
    carrier = payload.get("carrier")
    if (payload.get("target") != request["target"] or payload.get("body") != request["body"]
            or payload.get("digest") != request["digest"] or payload.get("tool") != request["tool"]
            or not isinstance(carrier, str) or not CARRIER_ID.fullmatch(carrier)):
        raise ValueError(UNCONFIRMED)
    return {
        "body": payload["body"],
        "carrier": carrier,
        "delivered": payload["delivered"],
        "digest": payload["digest"],
        "source": "tool",
        "target": payload["target"],
        "tool": payload["tool"],
    }


def _stored_for(file: Any, digest: str) -> dict[str, Any] | None:
    file.seek(0)
    for raw in file.read().splitlines():
        if not raw.strip():
            continue
        stored = json.loads(raw)
        if isinstance(stored, dict) and stored.get("digest") == digest:
            return stored
    return None


def deliver(draft: Mapping[str, Any], outbox: Path) -> dict[str, Any]:
    """Ask the carrier, then record that response and read it back.

    The outbox does not mark a message delivered. A missing carrier, a refusal,
    or a response for a different draft leaves no success record. A second call
    with the same digest returns the stored carrier receipt and does not ask again.
    """
    if not draft_binding_ok(draft):
        raise ValueError("The outbound draft is not bound")
    _carrier_target()
    path = Path(outbox)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("The outbound outbox must be a private file")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    digest = str(draft.get("digest") or "")
    handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "r+", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        fcntl.flock(file, fcntl.LOCK_EX)
        existing = _stored_for(file, digest)
        if existing is not None and receipt_matches(draft, existing):
            return existing
        record = _ask_carrier(draft)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        file.seek(0, os.SEEK_END)
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())
        found = _stored_for(file, digest)
        if found is None or found.get("carrier") != record["carrier"] or found.get("delivered") is not True:
            raise ValueError("The outbound delivery was not recorded")
        return found
