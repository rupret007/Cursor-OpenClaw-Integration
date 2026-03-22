#!/usr/bin/env python3
"""
Append a lockstep event for a Cursor job (CLI helper).

Usage:
  python3 scripts/andrea_sync_cursor_report.py --task-id tsk_... --event JobStarted --payload '{"cursor_agent_id":"bc-..."}'

Requires ANDREA_SYNC_URL and ANDREA_SYNC_INTERNAL_TOKEN, or pass --db to write SQLite directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.andrea_sync.adapters.cursor import cursor_event_command  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.store import connect, migrate  # noqa: E402
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--event", required=True, help="EventType name, e.g. JobStarted")
    ap.add_argument("--payload", default="{}", help="JSON object")
    ap.add_argument("--db", default="", help="SQLite path (bypass HTTP)")
    args = ap.parse_args()
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError:
        print("invalid --payload JSON", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("--payload must decode to a JSON object", file=sys.stderr)
        return 2
    cmd = cursor_event_command(args.task_id, args.event, payload)
    if args.db:
        dbp = Path(args.db).expanduser()
        conn = connect(dbp)
        migrate(conn)
        out = handle_command(conn, cmd)
    else:
        import urllib.error
        import urllib.request

        base = (os.environ.get("ANDREA_SYNC_URL") or "").strip().rstrip("/")
        tok = os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN", "")
        if not base or not tok:
            print("Need ANDREA_SYNC_URL + ANDREA_SYNC_INTERNAL_TOKEN or --db", file=sys.stderr)
            return 2
        inner = {
            "task_id": args.task_id,
            "event_type": args.event,
            "payload": payload,
        }
        data = json.dumps(inner).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/internal/events",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {tok}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    out = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"non-JSON response: {raw[:500]}", file=sys.stderr)
                    return 1
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(body or str(e), file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            print(str(e), file=sys.stderr)
            return 1
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
