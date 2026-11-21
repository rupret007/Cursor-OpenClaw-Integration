#!/usr/bin/env python3
"""Fence untrusted text so it cannot close itself or become a tool call.

The fence token is random for each call. A blob that contains the closer is
refused. Hidden direction and zero-width characters are refused. Fenced text
is data: a tool parser that honors `visible_to_tools` does not see it as a call.
"""

import re
import secrets


NAME = re.compile(r"[a-z]{1,16}")
TOKEN = re.compile(r"[0-9a-f]{16}")
HIDDEN = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")
FENCE = re.compile(
    r"\A<<UNTRUSTED ([a-z]{1,16}) ([0-9a-f]{16})>>\n(.*)\n<<END \1 \2>>\Z",
    re.DOTALL,
)


def fence(name, text, limit, token=None):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("The untrusted text has no fence")
    if not isinstance(text, str):
        raise ValueError("The untrusted text is not usable")
    if HIDDEN.search(text):
        raise ValueError("The untrusted text hides characters")
    if len(text) > limit:
        raise ValueError("The untrusted text is too long")
    marker = secrets.token_hex(8) if token is None else token
    if not isinstance(marker, str) or not TOKEN.fullmatch(marker):
        raise ValueError("The untrusted text has no fence")
    closer = f"<<END {name} {marker}>>"
    if closer in text or "<<UNTRUSTED " in text:
        raise ValueError("The untrusted text closed its fence")
    return f"<<UNTRUSTED {name} {marker}>>\n{text}\n{closer}"


def fenced(text):
    return isinstance(text, str) and FENCE.fullmatch(text) is not None


def inner_text(text):
    """Return the visible text inside one complete fence."""
    match = FENCE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return "" if text is None else str(text)
    return match.group(3)


def visible_to_tools(text):
    """Fenced text is data. Anything else can still reach the tool gate."""
    return not fenced(text)
