"""Today-schedule lookup with bounded fallbacks."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..store import list_upcoming_reminders_for_principal


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start_at: dt.datetime
    end_at: dt.datetime | None = None
    source: str = "calendar"
    calendar_name: str = ""


def _resolve_timezone(timezone_name: str = "") -> dt.tzinfo:
    raw = str(timezone_name or "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _today_bounds(*, now: dt.datetime, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    localized = now.astimezone(tz)
    start = localized.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start, end


def _parse_iso_datetime(raw: Any, *, tz: dt.tzinfo) -> dt.datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _load_events_from_json_value(raw: Any, *, tz: dt.tzinfo) -> list[CalendarEvent]:
    if not isinstance(raw, list):
        return []
    events: list[CalendarEvent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = _parse_iso_datetime(item.get("start") or item.get("start_at"), tz=tz)
        if start is None:
            continue
        end = _parse_iso_datetime(item.get("end") or item.get("end_at"), tz=tz)
        title = str(item.get("title") or item.get("summary") or "").strip() or "Untitled event"
        events.append(
            CalendarEvent(
                title=title,
                start_at=start,
                end_at=end,
                source=str(item.get("source") or "calendar").strip() or "calendar",
                calendar_name=str(item.get("calendar") or item.get("calendar_name") or "").strip(),
            )
        )
    return events


def _load_events_from_env(*, tz: dt.tzinfo) -> tuple[list[CalendarEvent], bool]:
    raw_json = (os.environ.get("ANDREA_CALENDAR_EVENTS_JSON") or "").strip()
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            return [], False
        return _load_events_from_json_value(payload, tz=tz), True
    raw_path = (os.environ.get("ANDREA_CALENDAR_EVENTS_PATH") or "").strip()
    if not raw_path:
        return [], False
    path = Path(raw_path).expanduser()
    if not path.exists():
        return [], False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], False
    return _load_events_from_json_value(payload, tz=tz), True


def _load_events_from_macos_calendar(*, tz: dt.tzinfo) -> tuple[list[CalendarEvent], bool]:
    if sys.platform != "darwin":
        return [], False
    if str(os.environ.get("ANDREA_CALENDAR_DISABLE_MACOS") or "").strip().lower() in {"1", "true", "yes"}:
        return [], False
    script = """
const app = Application('Calendar');
const calendars = app.calendars();
const now = new Date();
const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0);
const end = new Date(start.getTime());
end.setDate(end.getDate() + 1);
const events = [];
for (const cal of calendars) {
  for (const ev of cal.events()) {
    const startDate = ev.startDate();
    if (startDate >= start && startDate < end) {
      const endDate = ev.endDate();
      events.push({
        title: ev.summary(),
        start: startDate.toISOString(),
        end: endDate ? endDate.toISOString() : '',
        calendar: cal.name(),
        source: 'macos_calendar'
      });
    }
  }
}
JSON.stringify(events);
""".strip()
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except Exception:
        return [], False
    if proc.returncode != 0:
        return [], False
    try:
        payload = json.loads((proc.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return [], False
    return _load_events_from_json_value(payload, tz=tz), True


def _format_clock(value: dt.datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0") or value.strftime("%H:%M")


def _format_time_range(event: CalendarEvent) -> str:
    start = _format_clock(event.start_at)
    if event.end_at is None:
        return start
    return f"{start} to {_format_clock(event.end_at)}"


def _load_today_reminders(
    conn: Any,
    *,
    principal_id: str,
    now: dt.datetime,
    tz: dt.tzinfo,
) -> list[CalendarEvent]:
    pid = str(principal_id or "").strip()
    if not pid:
        return []
    reminders = list_upcoming_reminders_for_principal(
        conn,
        pid,
        now_ts=now.timestamp(),
        horizon_seconds=86400.0,
        limit=12,
    )
    start, end = _today_bounds(now=now, tz=tz)
    events: list[CalendarEvent] = []
    for item in reminders:
        due_at = float(item.get("due_at") or 0.0)
        if due_at <= 0:
            continue
        due = dt.datetime.fromtimestamp(due_at, tz=tz)
        if due < start or due >= end:
            continue
        title = str(item.get("message") or "").strip()
        if not title:
            continue
        events.append(CalendarEvent(title=title, start_at=due, source="reminder"))
    return events


def build_today_schedule_reply(
    conn: Any,
    *,
    principal_id: str,
    timezone_name: str = "",
    now: dt.datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    tz = _resolve_timezone(timezone_name)
    current = now.astimezone(tz) if isinstance(now, dt.datetime) else dt.datetime.now(tz)
    start, end = _today_bounds(now=current, tz=tz)
    provider_events, provider_available = _load_events_from_env(tz=tz)
    if not provider_events:
        macos_events, macos_available = _load_events_from_macos_calendar(tz=tz)
        provider_events = macos_events
        provider_available = provider_available or macos_available
    provider_events = [event for event in provider_events if start <= event.start_at < end]
    reminder_events = _load_today_reminders(conn, principal_id=principal_id, now=current, tz=tz)
    all_events = sorted(provider_events + reminder_events, key=lambda item: item.start_at)
    metadata = {
        "provider_available": provider_available,
        "calendar_event_count": len(provider_events),
        "reminder_event_count": len(reminder_events),
        "timezone": str(getattr(tz, "key", tz)),
    }
    if not all_events:
        if provider_available:
            return "Nothing scheduled today.", metadata
        return "I don't see anything scheduled today.", metadata
    lines = ["Here’s what you have today:"]
    for event in all_events[:8]:
        suffix = ""
        if event.source == "reminder":
            suffix = " (reminder)"
        elif event.calendar_name:
            suffix = f" ({event.calendar_name})"
        lines.append(f"- {_format_time_range(event)}: {event.title}{suffix}")
    if len(all_events) > 8:
        lines.append(f"- Plus {len(all_events) - 8} more item(s) later today.")
    return "\n".join(lines), metadata
