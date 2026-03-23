#!/usr/bin/env python3
"""Run Andrea's deterministic experience assurance replay against the local lockstep DB."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.experience_assurance import run_experience_assurance  # noqa: E402
from services.andrea_sync.store import connect, migrate  # noqa: E402


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
    parser.add_argument("--actor", default="script")
    parser.add_argument("--source-task-id", default="")
    parser.add_argument("--repair-on-fail", action="store_true", default=False)
    parser.add_argument("--cursor-execute", action="store_true", default=False)
    parser.add_argument("--no-save", action="store_true", default=False)
    parser.add_argument("--no-write-report", action="store_true", default=False)
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        migrate(conn)
        payload = run_experience_assurance(
            conn,
            actor=str(args.actor or "script"),
            repo_path=Path(args.repo).expanduser(),
            save_run=not bool(args.no_save),
            repair_on_fail=bool(args.repair_on_fail),
            cursor_execute=bool(args.cursor_execute),
            source_task_id=str(args.source_task_id or ""),
            write_report=not bool(args.no_write_report),
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok") and payload.get("verification_report", {}).get("passed") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
