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
import stat


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


DISABLED = {
    "structured_live_news": "Live news lookup is off until an allowlisted source is configured.",
    "structured_grounded_research": "Grounded research lookup is off until an allowlisted source is configured.",
    "structured_recent_text_messages": (
        "Recent-message lookup is off until a local message source is configured. "
        "It does not contact a messaging service."
    ),
    "structured_personal_assistant_synthesis": (
        "Personal-assistant synthesis is off until assistant-state lines are supplied."
    ),
}


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


def _visible(page):
    match = sanitize.FENCE.fullmatch(page)
    if match is None or sanitize.visible_to_tools(page):
        raise ValueError("Fetched text was not fenced")
    return match.group(3)


def _finish(allowed, refused, invoked, fenced_pages, *, status, summary, blocked):
    """Return one lookup. Fenced pages are data and are not admitted as tool calls."""
    for page in fenced_pages:
        _visible(page)
    text = summary.strip()
    return {
        "ok": status == "completed",
        "backend": "restricted_lookup",
        "execution_lane": "restricted_lookup",
        "delegated_to_cursor": False,
        "summary": text[:2000],
        "user_summary": text[:500],
        "collaboration_trace": [],
        "machine_collaboration_trace": [],
        "phase_outputs": {},
        "blocked_reason": blocked,
        "internal_trace": "",
        "raw_text": text[:4000],
        "tools_invoked": invoked,
        "tools_refused": refused,
        "allowed_tools": list(allowed),
        "fenced": fenced_pages[0] if fenced_pages else "",
        "fenced_pages": fenced_pages,
        "status": status,
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


def _state_lines(prompt):
    marker = "Assistant-state lines:"
    if marker not in prompt:
        return []
    lines = []
    for raw in prompt.split(marker, 1)[1].splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("- "):
            break
        text = line[2:].strip()
        if text:
            lines.append(text)
    return lines


def read_messages(path):
    """Read one local message file. Nothing is sent to a messaging service."""
    source = Path(path)
    if source.is_symlink():
        raise ValueError("The message source must be a private file")
    try:
        info = source.stat()
    except OSError:
        raise ValueError("The message source must be a private file") from None
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 64 * 1024:
        raise ValueError("The message source must be a private file")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("The message source is unreadable") from None
    if (not isinstance(document, dict) or set(document) != {"version", "messages"}
            or document.get("version") != 1):
        raise ValueError("The message source is unreadable")
    messages = document["messages"]
    if not isinstance(messages, list) or not 0 < len(messages) <= 20:
        raise ValueError("The message source is unreadable")
    fenced = []
    for item in messages:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise ValueError("The message source is unreadable")
        try:
            page = sanitize.fence("message", item, 2000)
        except ValueError:
            raise ValueError("The message source is unreadable") from None
        fenced.append(page)
        _visible(page)
    return fenced


def _extend_refused(refused, extra):
    for name in extra:
        if name not in refused:
            refused.append(name)


def run_lookup(route, prompt, *, allowlist=None, resolve=None, fetcher=None):
    """Run one lookup. The only network read is an allowlisted fetch. Nothing is sent."""
    allowed = tools_for(route)
    prompt = prompt if isinstance(prompt, str) else ""
    refused = _refused_outbound(route, prompt)
    document = allowlist if allowlist is not None else load_allowlist()
    if "fetch" in allowed:
        targets = []
        for url in document.get("sources", {}).get(route, ()):
            if url not in targets:
                targets.append(url)
        for url in URL.findall(prompt):
            if url not in targets:
                targets.append(url)
        if not targets:
            return _finish(allowed, refused, [], [], status="disabled",
                           summary=DISABLED[route], blocked="lookup_disabled")
        invoked = []
        fenced_pages = []
        for url in targets:
            try:
                admit(route, "fetch", url)
            except ValueError:
                if "fetch" not in refused:
                    refused.append("fetch")
                continue
            try:
                if fetcher is not None:
                    page = fetcher(url, document)
                else:
                    page = fetch.fetch(url, document, resolve=resolve or fetch.system_resolve)
            except (OSError, ValueError):
                if "fetch" not in refused:
                    refused.append("fetch")
                continue
            # The page is data. Do not admit it, and do not fetch any URL it names.
            fenced_pages.append(page)
            invoked.append("fetch")
            _extend_refused(refused, _refused_outbound(route, _visible(page)))
        if not fenced_pages:
            return _finish(allowed, refused, [], [], status="refused",
                           summary="", blocked="lookup_tools_restricted")
        summary = " ".join(_visible(page) for page in fenced_pages)
        return _finish(allowed, refused, invoked, fenced_pages, status="completed",
                       summary=summary, blocked="")
    if "read_messages" in allowed:
        source = os.environ.get("FO_MESSAGE_SOURCE", "")
        if not source:
            return _finish(allowed, refused, [], [], status="disabled",
                           summary=DISABLED[route], blocked="lookup_disabled")
        try:
            admit(route, "read_messages", source)
            fenced_pages = read_messages(source)
        except (OSError, ValueError):
            return _finish(allowed, refused, [], [], status="refused",
                           summary="", blocked="lookup_tools_restricted")
        for page in fenced_pages:
            _extend_refused(refused, _refused_outbound(route, _visible(page)))
        summary = " ".join(_visible(page) for page in fenced_pages)
        return _finish(allowed, refused, ["read_messages"], fenced_pages, status="completed",
                       summary=summary, blocked="")
    kept = []
    for line in _state_lines(prompt):
        try:
            page = sanitize.fence("state", line, 2000)
        except ValueError:
            continue
        kept.append(page)
        _extend_refused(refused, _refused_outbound(route, line))
    if not kept:
        return _finish(allowed, refused, [], [], status="disabled",
                       summary=DISABLED[route], blocked="lookup_disabled")
    summary = " ".join(_visible(page) for page in kept)
    return _finish(allowed, refused, [], kept, status="completed",
                   summary=summary, blocked="")


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
