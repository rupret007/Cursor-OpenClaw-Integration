"""Tests for Trusted Follow-Through and Closure Manager (Stage A)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.assistant_followthrough import (
    effective_followthrough_pack_status,
    followthrough_metrics_rollup,
    on_reminder_lifecycle_event,
    set_followthrough_pack_status_override,
    sync_after_user_outcome_receipt,
)
from services.andrea_sync.closure_rules import classify_daily_pack_receipt
from services.andrea_sync.schema import EventType
from services.andrea_sync.store import connect, create_task, list_recent_closure_decisions, migrate


@pytest.fixture()
def conn():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        c = connect(db)
        migrate(c)
        tid = f"tsk_{uuid.uuid4().hex[:12]}"
        create_task(c, tid, "telegram")
        yield c


def test_classify_status_awaiting_approval():
    r = classify_daily_pack_receipt(
        scenario_id="statusFollowupContinue",
        receipt_kind="status_followup",
        delivery_state="n/a",
        next_step="",
        reply_reason="plan_awaiting_approval",
    )
    assert r["closure_state"] == "awaiting_user"
    assert r["loop_kind"] == "approval_wait"


def test_classify_reminder_scheduled():
    r = classify_daily_pack_receipt(
        scenario_id="noteOrReminderCapture",
        receipt_kind="reminder_created",
        delivery_state="scheduled",
        next_step="",
        proof_refs={"due_at": time.time() + 3600},
    )
    assert r["closure_state"] == "awaiting_delivery"
    assert r["needs_continuation_signal"] is True


def test_sync_receipt_creates_open_loop_and_closure(conn):
    os.environ["ANDREA_FOLLOWTHROUGH_PACK_STATUS"] = "tracked_only"
    tid = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()["task_id"]
    rid = f"rcpt_test_{uuid.uuid4().hex[:10]}"
    conn.execute(
        """
        INSERT INTO user_outcome_receipts(
            receipt_id, task_id, goal_id, scenario_id, pack_id, receipt_kind, summary,
            proof_refs_json, delivery_state, next_step, pass_hint, created_at, payload_json,
            closure_state, closure_proof_id, followthrough_kind
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rid,
            tid,
            "",
            "recentMessagesOrInboxLookup",
            "trusted_daily_continuity_v1",
            "inbox_lookup",
            "summary",
            "{}",
            "read_only_summary",
            "",
            1,
            time.time(),
            "{}",
            "",
            "",
            "",
        ),
    )
    conn.commit()

    out = sync_after_user_outcome_receipt(
        conn,
        receipt_id=rid,
        task_id=tid,
        goal_id="",
        scenario_id="recentMessagesOrInboxLookup",
        pack_id="trusted_daily_continuity_v1",
        receipt_kind="inbox_lookup",
        summary="summary",
        delivery_state="read_only_summary",
        next_step="",
        proof_refs={},
        payload={},
    )
    assert out and out.get("ok") is True
    rows = list_recent_closure_decisions(conn, limit=5)
    assert any(str(r["closure_state"]) == "completed" for r in rows)


def test_operator_override_status(conn):
    r = set_followthrough_pack_status_override(
        conn, status="frozen", actor="tester", reason="unit"
    )
    assert r.get("ok") is True
    assert effective_followthrough_pack_status(conn) == "frozen"


def test_reminder_failed_needs_repair(conn):
    tid = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()["task_id"]
    out = on_reminder_lifecycle_event(
        conn,
        task_id=tid,
        event_name="failed",
        payload={"reminder_id": "r1"},
    )
    assert out and out["closure_state"] == "needs_repair"


def test_followthrough_metrics(conn):
    os.environ["ANDREA_FOLLOWTHROUGH_PACK_STATUS"] = "shadow_followthrough"
    tid = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()["task_id"]
    sync_after_user_outcome_receipt(
        conn,
        receipt_id=f"rcpt_{uuid.uuid4().hex[:10]}",
        task_id=tid,
        goal_id="",
        scenario_id="statusFollowupContinue",
        pack_id="trusted_daily_continuity_v1",
        receipt_kind="status_followup",
        summary="ok",
        delivery_state="n/a",
        next_step="",
        proof_refs={},
        payload={},
        reply_reason="direct",
    )
    m = followthrough_metrics_rollup(conn, window_seconds=3600.0)
    assert m.get("closure_decision_count", 0) >= 1


def test_open_loop_event_recorded(conn):
    os.environ["ANDREA_FOLLOWTHROUGH_PACK_STATUS"] = "tracked_only"
    tid = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()["task_id"]
    sync_after_user_outcome_receipt(
        conn,
        receipt_id=f"rcpt_{uuid.uuid4().hex[:10]}",
        task_id=tid,
        goal_id="",
        scenario_id="goalContinuationAcrossSessions",
        pack_id="trusted_daily_continuity_v1",
        receipt_kind="goal_resume",
        summary="resume",
        delivery_state="n/a",
        next_step="next",
        proof_refs={"goal_id": "g1"},
        payload={},
    )
    ev = conn.execute(
        """
        SELECT COUNT(*) AS c FROM events
        WHERE task_id = ? AND event_type = ?
        """,
        (tid, EventType.OPEN_LOOP_RECORDED.value),
    ).fetchone()
    assert int(ev["c"]) >= 1
