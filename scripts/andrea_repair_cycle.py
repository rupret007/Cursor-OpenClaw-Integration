#!/usr/bin/env python3
"""Run one incident-driven repair cycle against the local Andrea lockstep DB."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.repair_orchestrator import run_incident_repair_cycle  # noqa: E402
from services.andrea_sync.store import connect, migrate  # noqa: E402


def _load_json_file(path: str) -> dict[str, object]:
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    payload = json.loads(raw)
    return dict(payload) if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("ANDREA_SYNC_DB", str(REPO_ROOT / "data/andrea_sync.db")),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("ANDREA_SYNC_CURSOR_REPO", str(REPO_ROOT)),
    )
    parser.add_argument("--incident-json", default="")
    parser.add_argument("--verification-report-json", default="")
    parser.add_argument("--source-task-id", default="")
    parser.add_argument("--cursor-execute", action="store_true", default=False)
    parser.add_argument("--no-write-report", action="store_true", default=False)
    args = parser.parse_args()

    incident_payload = _load_json_file(args.incident_json) if args.incident_json else {}
    verification_report = (
        _load_json_file(args.verification_report_json) if args.verification_report_json else {}
    )

    conn = connect(Path(args.db))
    try:
        migrate(conn)
        payload = run_incident_repair_cycle(
            conn,
            repo_path=Path(args.repo).expanduser(),
            actor="script",
            incident_payload=incident_payload,
            verification_report=verification_report,
            source_task_id=str(args.source_task_id or ""),
            cursor_execute=bool(args.cursor_execute),
            write_report=not bool(args.no_write_report),
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
