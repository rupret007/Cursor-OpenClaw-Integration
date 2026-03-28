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
    parser.add_argument(
        "--suite",
        default="",
        help="Experience suite id (e.g. conversation_core, routing_matrix).",
    )
    parser.add_argument(
        "--llm-eval",
        action="store_true",
        default=False,
        help="When running conversation_core or routing_matrix, call verifier-slot LLM on weak/failed turns (needs OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--adjudicate-ambiguous",
        action="store_true",
        default=False,
        help="When running conversation_core or routing_matrix, run semantic adjudicator on ambiguous/weak cases.",
    )
    parser.add_argument(
        "--prepare-fix-brief",
        action="store_true",
        default=False,
        help="When running conversation_core or routing_matrix, attach gated Cursor fix briefs to run metadata.",
    )
    parser.add_argument(
        "--fix-brief-handoff",
        action="store_true",
        default=False,
        help="When --prepare-fix-brief is set, include cursor_handoff_ready brief candidates in metadata.",
    )
    parser.add_argument(
        "--scenario-ids",
        default="",
        help="Comma-separated case ids (no suite prefix) for conversation_core or routing_matrix subsets.",
    )
    parser.add_argument(
        "--sample-pass-evals",
        type=int,
        default=0,
        help="When --llm-eval is set, 1/N probability to evaluate passing cases (for calibration).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        default=False,
        help="With --suite conversation_core or routing_matrix, run a small smoke subset (fast harness check).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        default=False,
        help="With --suite conversation_core or routing_matrix, stop after the first failed scenario.",
    )
    args = parser.parse_args()

    suite = str(args.suite or "").strip() or None
    suite_l = str(suite or "").strip().lower()
    conversation_eval_options = {
        "llm_eval": bool(args.llm_eval),
        "adjudicate_ambiguous": bool(args.adjudicate_ambiguous),
        "prepare_fix_brief": bool(args.prepare_fix_brief),
        "fix_brief_handoff": bool(args.fix_brief_handoff),
        "sample_pass_evals": int(args.sample_pass_evals or 0),
        "smoke": bool(args.smoke),
        "fail_fast": bool(args.fail_fast),
        "scenario_ids": [x.strip() for x in str(args.scenario_ids or "").split(",") if x.strip()],
    }

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
            suite=suite,
            conversation_eval_options=conversation_eval_options
            if suite_l in ("conversation_core", "routing_matrix")
            else None,
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        report = payload.get("verification_report", {}) or {}
        passed = bool(report.get("passed"))
        if suite_l in ("conversation_core", "routing_matrix"):
            passed = bool((report.get("metadata") or {}).get("quality_passed", passed))
        return 0 if payload.get("ok") and passed else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
