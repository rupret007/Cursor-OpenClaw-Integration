"""Map Telegram Bot API Update objects to lockstep commands."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def update_to_command(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return a command body for bus.handle_command, or None if no user text to process.
    """
    uid = update.get("update_id")
    if uid is None:
        return None
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text") or msg.get("caption")
    if not text or not str(text).strip():
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = (msg.get("from") or {}).get("id")
    return {
        "command_type": "SubmitUserMessage",
        "channel": "telegram",
        "external_id": str(uid),
        "payload": {
            "text": str(text).strip(),
            "chat_id": chat_id,
            "from_user": from_user,
        },
    }


def verify_webhook_secret(query_secret: str, configured: str) -> bool:
    if not configured:
        return False
    return query_secret == configured
