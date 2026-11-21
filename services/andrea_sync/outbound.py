"""Bind one outbound draft to the exact delivery a tool actually performed."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping


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


def deliver(draft: Mapping[str, Any], outbox: Path) -> dict[str, Any]:
    """Record one delivery and return the receipt read back from that record.

    A second call with the same digest returns the stored receipt and does not
    append another delivery.
    """
    if not draft_binding_ok(draft):
        raise ValueError("The outbound draft is not bound")
    path = Path(outbox)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("The outbound outbox must be a private file")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    tool = str(draft.get("skill_key") or "")
    if tool not in {"bluebubbles", "send_message"}:
        tool = "send_message"
    record = {
        "body": str(draft.get("message") or ""),
        "delivered": True,
        "digest": str(draft.get("digest") or ""),
        "source": "tool",
        "target": str(draft.get("target") or ""),
        "tool": tool,
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "r+", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        for raw in file.read().splitlines():
            if not raw.strip():
                continue
            stored = json.loads(raw)
            if isinstance(stored, dict) and stored.get("digest") == record["digest"]:
                return stored
        file.seek(0, os.SEEK_END)
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())
        file.seek(0)
        found = None
        for raw in file.read().splitlines():
            if not raw.strip():
                continue
            stored = json.loads(raw)
            if isinstance(stored, dict) and stored.get("digest") == record["digest"]:
                found = stored
        if found is None:
            raise ValueError("The outbound delivery was not recorded")
        return found
