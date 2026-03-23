#!/usr/bin/env python3
"""Run one Andrea autonomous optimization cycle against the local lockstep DB."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.optimizer import (  # noqa: E402
    apply_ready_proposals,
    run_optimization_cycle,
)
from services.andrea_sync.store import connect, migrate  # noqa: E402


def _run_regressions(command: str, *, cwd: Path) -> dict[str, object]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
    total = 0
    match = re.search(r"\bRan\s+(\d+)\s+tests?\b", output)
    if match:
        total = int(match.group(1))
    return {
        "passed": proc.returncode == 0,
        "total": total,
        "command": command,
        "exit_code": proc.returncode,
        "output_excerpt": output[:2000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("ANDREA_SYNC_DB", str(REPO_ROOT / "data/andrea_sync.db")))
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--require-skill", action="append", default=[])
    parser.add_argument(
        "--analysis-mode",
        default="heuristic",
        choices=["heuristic", "openclaw_prompt", "gemini_background"],
    )
    parser.add_argument("--repo", default=os.environ.get("ANDREA_SYNC_CURSOR_REPO", str(REPO_ROOT)))
    parser.add_argument(
        "--background-idle-seconds",
        type=float,
        default=float(os.environ.get("ANDREA_SYNC_BACKGROUND_OPTIMIZER_IDLE_SECONDS", "120")),
    )
    parser.add_argument(
        "--regression-command",
        default="python3 -m unittest discover -p 'test_*.py'",
    )
    parser.add_argument(
        "--regression-cwd",
        default=str(REPO_ROOT / "tests"),
    )
    emit_group = parser.add_mutually_exclusive_group()
    emit_group.add_argument("--emit-proposals", dest="emit_proposals", action="store_true")
    emit_group.add_argument("--no-emit-proposals", dest="emit_proposals", action="store_false")
    parser.set_defaults(emit_proposals=True)
    parser.add_argument("--skip-regressions", action="store_true", default=False)
    parser.add_argument("--regression-passed", action="store_true", default=False)
    parser.add_argument("--regression-total", type=int, default=0)
    parser.add_argument("--auto-apply-ready", action="store_true", default=False)
    parser.add_argument("--auto-apply-limit", type=int, default=1)
    args = parser.parse_args()

    conn = connect(Path(args.db))
    try:
        migrate(conn)
        if args.skip_regressions:
            regression_report = {
                "passed": bool(args.regression_passed),
                "total": int(args.regression_total),
                "command": "",
            }
        else:
            regression_report = _run_regressions(
                str(args.regression_command),
                cwd=Path(args.regression_cwd).expanduser(),
            )
            if args.regression_total > 0:
                regression_report["total"] = int(args.regression_total)
            if args.regression_passed:
                regression_report["passed"] = True
        payload = run_optimization_cycle(
            conn,
            limit=max(1, int(args.limit)),
            regression_report=regression_report,
            required_skills=[str(v) for v in args.require_skill if str(v).strip()],
            emit_proposals=bool(args.emit_proposals),
            actor="script",
            analysis_mode=str(args.analysis_mode),
            repo_path=Path(args.repo).expanduser(),
            auto_apply_ready=bool(args.auto_apply_ready and args.analysis_mode == "gemini_background"),
            idle_seconds=float(args.background_idle_seconds),
        )
        payload["regression_report"] = regression_report
        if args.auto_apply_ready and payload.get("ok") and args.analysis_mode != "gemini_background":
            payload["auto_heal"] = apply_ready_proposals(
                conn,
                proposals=payload.get("proposals") if isinstance(payload.get("proposals"), list) else [],
                repo_path=Path(args.repo).expanduser(),
                actor="script",
                max_apply=max(1, int(args.auto_apply_limit)),
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
