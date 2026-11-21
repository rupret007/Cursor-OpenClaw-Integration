#!/usr/bin/env python3
"""Route allowlist for lookup. Fetched pages stay fenced and cannot select a tool.

Lookup routes name the only tools they may call. Those names do not include an
outbound skill. `restricted_fetch` returns a fence, and `visible_to_tools` is
false for that fence, so the page is data rather than a tool call.
"""

import importlib.util
import json
import os
from pathlib import Path
import re


def _load(name, filename):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize = _load("fo_guard_sanitize_boundary", "sanitize.py")
fetch = _load("fo_guard_fetch_boundary", "restricted_fetch.py")

LOOKUP_ROUTES = {
    "structured_live_news": ("fetch",),
    "structured_grounded_research": ("fetch",),
    "structured_recent_text_messages": ("read_messages",),
    "structured_personal_assistant_synthesis": (),
}
OUTBOUND_TOOLS = frozenset({
    "send_message", "bluebubbles", "imessage", "telegram", "exec", "cursor_handoff", "sms",
})
OUTBOUND_WORD = re.compile(
    r"\b(?:send_message|bluebubbles|imessage|telegram|cursor_handoff|\bexec\b|sms)\b",
    re.IGNORECASE,
)
URL = re.compile(r"https?://[^\s\"'<>]+")


def is_lookup(route):
    return route in LOOKUP_ROUTES


def tools_for(route):
    if route not in LOOKUP_ROUTES:
        raise ValueError("This route has no tool allowlist")
    allowed = tuple(LOOKUP_ROUTES[route])
    if any(name in OUTBOUND_TOOLS for name in allowed):
        raise ValueError("A lookup route cannot allow an outbound tool")
    return allowed


def admit(route, tool, argument):
    """Accept one tool call for this route. Fenced text and outbound tools are refused."""
    allowed = tools_for(route)
    if tool in OUTBOUND_TOOLS or tool not in allowed:
        raise ValueError("This route cannot use that tool")
    if not isinstance(argument, str) or not sanitize.visible_to_tools(argument):
        raise ValueError("Fenced text is not a tool call")
    return True


def data_notes(notes):
    """Keep fenced notes. An unfenced note is omitted and is not returned raw."""
    kept = []
    for note in notes or []:
        if isinstance(note, str) and note and sanitize.fenced(note) and not sanitize.visible_to_tools(note):
            kept.append(note)
    return kept


def load_allowlist(path=None):
    raw = path if path is not None else os.environ.get("FO_FETCH_ALLOWLIST", "")
    if not raw:
        return fetch.load_allowlist({"version": 1, "hosts": [], "fixtures": []})
    document = json.loads(Path(raw).read_text(encoding="utf-8"))
    return fetch.load_allowlist(document)


def _refused_outbound(route, text):
    refused = []
    seen = set()
    for match in OUTBOUND_WORD.finditer(text or ""):
        name = match.group(0).lower()
        if name in seen:
            continue
        seen.add(name)
        try:
            admit(route, name, text)
        except ValueError:
            refused.append(name)
        else:
            raise ValueError("An outbound tool was admitted on a lookup route")
    return refused


def run_lookup(route, prompt, *, allowlist=None, resolve=None, fetcher=None):
    """Run one lookup. The only network read is an allowlisted fetch. Nothing is sent."""
    allowed = tools_for(route)
    prompt = prompt if isinstance(prompt, str) else ""
    refused = _refused_outbound(route, prompt)
    invoked = []
    fenced_pages = []
    document = allowlist if allowlist is not None else load_allowlist()
    if "fetch" in allowed:
        for url in URL.findall(prompt):
            try:
                admit(route, "fetch", url)
            except ValueError:
                refused.append("fetch")
                continue
            try:
                if fetcher is not None:
                    page = fetcher(url, document)
                else:
                    page = fetch.fetch(url, document, resolve=resolve or fetch.system_resolve)
            except (OSError, ValueError):
                refused.append("fetch")
                continue
            if not sanitize.fenced(page) or sanitize.visible_to_tools(page):
                raise ValueError("Fetched text was not fenced")
            # Names inside the fence are not tool calls. Do not admit them.
            if sanitize.visible_to_tools(page):
                admit(route, "send_message", page)
            fenced_pages.append(page)
            invoked.append("fetch")
    visible = []
    for page in fenced_pages:
        match = sanitize.FENCE.fullmatch(page)
        if match:
            visible.append(match.group(3))
    summary = " ".join(visible).strip()
    return {
        "ok": bool(fenced_pages),
        "backend": "restricted_lookup",
        "execution_lane": "restricted_lookup",
        "delegated_to_cursor": False,
        "summary": summary[:2000],
        "user_summary": summary[:500],
        "collaboration_trace": [],
        "machine_collaboration_trace": [],
        "phase_outputs": {},
        "blocked_reason": "" if fenced_pages else "lookup_tools_restricted",
        "internal_trace": "",
        "raw_text": summary[:4000],
        "tools_invoked": invoked,
        "tools_refused": refused,
        "allowed_tools": list(allowed),
        "fenced": fenced_pages[0] if len(fenced_pages) == 1 else "",
        "fenced_pages": fenced_pages,
        "status": "completed" if fenced_pages else "refused",
        "stop_reason": "",
        "openclaw_run_id": "",
        "openclaw_session_id": "",
        "provider": "",
        "model": "",
        "cursor_agent_id": "",
        "agent_url": "",
        "pr_url": "",
        "collaboration_mode": "restricted",
        "preferred_model_family": "",
        "preferred_model_label": "",
        "requested_session_id": "",
    }


def lookup_result(route, prompt, *, session_id="", collaboration_mode="", preferred_model_family="",
                  preferred_model_label="", allowed_tools=None):
    """Lookup entry used by the hybrid runner. A wider allowlist than the route is refused."""
    expected = tools_for(route)
    if allowed_tools is not None:
        given = tuple(part for part in str(allowed_tools).split(",") if part)
        if given != expected:
            raise ValueError("This route cannot use that tool")
    result = run_lookup(route, prompt)
    result["requested_session_id"] = session_id
    result["collaboration_mode"] = collaboration_mode or "restricted"
    result["preferred_model_family"] = preferred_model_family
    result["preferred_model_label"] = preferred_model_label
    return result
