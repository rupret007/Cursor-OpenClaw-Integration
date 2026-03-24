"""SQLite WAL event store for Andrea lockstep."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401 used in list_tasks

from .experience_types import new_experience_run_id
from .schema import Channel, EventType


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            ts REAL NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, seq);
        CREATE TABLE IF NOT EXISTS idempotency (
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS principals (
            principal_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS principal_links (
            channel TEXT NOT NULL,
            external_key TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(channel, external_key)
        );
        CREATE INDEX IF NOT EXISTS idx_principal_links_principal
            ON principal_links(principal_id, channel);
        CREATE TABLE IF NOT EXISTS task_principals (
            task_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_principals_principal
            ON task_principals(principal_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS principal_memories (
            memory_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            source_task_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_principal_memories_principal
            ON principal_memories(principal_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS principal_preferences (
            principal_id TEXT NOT NULL,
            pref_key TEXT NOT NULL,
            pref_value_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(principal_id, pref_key)
        );
        CREATE TABLE IF NOT EXISTS reminders (
            reminder_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            delivery_target TEXT NOT NULL,
            message TEXT NOT NULL,
            due_at REAL NOT NULL,
            status TEXT NOT NULL,
            source_task_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(status, due_at ASC);
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            source_task_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            error_type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            incident_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_status_updated
            ON incidents(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
            ON incidents(fingerprint, updated_at DESC);
        CREATE TABLE IF NOT EXISTS repair_attempts (
            attempt_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
            attempt_no INTEGER NOT NULL,
            stage TEXT NOT NULL,
            model_used TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            attempt_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_repair_attempts_incident
            ON repair_attempts(incident_id, attempt_no ASC, updated_at ASC);
        CREATE TABLE IF NOT EXISTS repair_plans (
            plan_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            model_used TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_repair_plans_incident
            ON repair_plans(incident_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS experience_runs (
            run_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            passed INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            total_checks INTEGER NOT NULL DEFAULT 0,
            failed_checks INTEGER NOT NULL DEFAULT 0,
            average_score REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            run_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_experience_runs_updated
            ON experience_runs(updated_at DESC);
        CREATE TABLE IF NOT EXISTS experience_checks (
            check_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES experience_runs(run_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            status TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            check_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_experience_checks_run
            ON experience_checks(run_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS goals (
            goal_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'internal',
            status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goals_principal_status
            ON goals(principal_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS goal_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE,
            ts REAL NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_goal_events_goal_ts ON goal_events(goal_id, seq);
        CREATE TABLE IF NOT EXISTS task_goals (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
            goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_goals_goal ON task_goals(goal_id);
        CREATE TABLE IF NOT EXISTS goal_artifacts (
            artifact_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'file',
            label TEXT NOT NULL DEFAULT '',
            uri TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goal_artifacts_goal
            ON goal_artifacts(goal_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS goal_approvals (
            approval_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            rationale TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goal_approvals_goal
            ON goal_approvals(goal_id, status);
        CREATE TABLE IF NOT EXISTS workflows (
            workflow_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            name TEXT NOT NULL DEFAULT '',
            definition_json TEXT NOT NULL DEFAULT '{}',
            next_run_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflows_principal
            ON workflows(principal_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS execution_attempts (
            exec_attempt_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            goal_id TEXT NOT NULL DEFAULT '',
            lane TEXT NOT NULL DEFAULT '',
            backend TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            handle_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            parent_attempt_id TEXT NOT NULL DEFAULT '',
            continuation_state TEXT NOT NULL DEFAULT '',
            verification_state TEXT NOT NULL DEFAULT '',
            recovery_state TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_synced_at REAL NOT NULL DEFAULT 0,
            completed_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_execution_attempts_task_status
            ON execution_attempts(task_id, status);
        CREATE INDEX IF NOT EXISTS idx_execution_attempts_goal
            ON execution_attempts(goal_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS execution_plans (
            plan_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            principal_id TEXT NOT NULL DEFAULT '',
            intent_summary TEXT NOT NULL DEFAULT '',
            plan_kind TEXT NOT NULL DEFAULT 'delegated_repo_task',
            status TEXT NOT NULL DEFAULT 'draft',
            risk_tier INTEGER NOT NULL DEFAULT 2,
            approval_state TEXT NOT NULL DEFAULT 'none',
            verification_state TEXT NOT NULL DEFAULT 'pending',
            recovery_state TEXT NOT NULL DEFAULT '',
            current_step_id TEXT NOT NULL DEFAULT '',
            router_snapshot_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_execution_plans_goal
            ON execution_plans(goal_id, status);
        CREATE INDEX IF NOT EXISTS idx_execution_plans_task
            ON execution_plans(task_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS plan_steps (
            step_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL REFERENCES execution_plans(plan_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            step_kind TEXT NOT NULL DEFAULT '',
            lane TEXT NOT NULL DEFAULT '',
            action_json TEXT NOT NULL DEFAULT '{}',
            policy_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            execution_attempt_id TEXT NOT NULL DEFAULT '',
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            recovery_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id, ordinal);
        CREATE TABLE IF NOT EXISTS verification_results (
            verification_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL DEFAULT '',
            step_id TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_verification_results_plan
            ON verification_results(plan_id, step_id);
        CREATE TABLE IF NOT EXISTS collaboration_activation_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            collab_id TEXT NOT NULL DEFAULT '',
            scenario_id TEXT NOT NULL,
            trigger TEXT NOT NULL,
            ts REAL NOT NULL,
            activation_mode TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_collab_activation_task_ts
            ON collaboration_activation_decisions(task_id, ts DESC);
        CREATE TABLE IF NOT EXISTS collaboration_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            collab_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            trigger TEXT NOT NULL,
            ts REAL NOT NULL,
            canonical_class TEXT NOT NULL,
            usefulness_detail TEXT NOT NULL DEFAULT '',
            live_advisory_ran INTEGER NOT NULL DEFAULT 0,
            role_invocation_delta INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_collab_outcomes_scen_trig
            ON collaboration_outcomes(scenario_id, trigger, ts DESC);
        CREATE TABLE IF NOT EXISTS repair_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            collab_id TEXT NOT NULL,
            ts REAL NOT NULL,
            action_type TEXT NOT NULL,
            executed INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_repair_outcomes_task_ts
            ON repair_outcomes(task_id, ts DESC);
        CREATE TABLE IF NOT EXISTS collaboration_promotion_revisions (
            revision_id TEXT PRIMARY KEY,
            subject_key TEXT NOT NULL,
            promotion_level TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            operator_ack INTEGER NOT NULL DEFAULT 0,
            fallback_revision_id TEXT NOT NULL DEFAULT '',
            evidence_snapshot_json TEXT NOT NULL DEFAULT '{}',
            risk_notes TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_collab_promo_subject_status_created
            ON collaboration_promotion_revisions(subject_key, status, created_at DESC);
        CREATE TABLE IF NOT EXISTS collaboration_promotion_rollbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            observed_json TEXT NOT NULL DEFAULT '{}',
            fallback_revision_id TEXT NOT NULL DEFAULT '',
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_collab_promo_rb_subject_ts
            ON collaboration_promotion_rollbacks(subject_key, ts DESC);
        CREATE TABLE IF NOT EXISTS collaboration_operator_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            action_kind TEXT NOT NULL,
            subject_key TEXT NOT NULL DEFAULT '',
            scenario_id TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT '',
            requested_level TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            revision_id TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_collab_op_actions_ts
            ON collaboration_operator_actions(ts DESC);
        CREATE TABLE IF NOT EXISTS scenario_onboarding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            onboarding_id TEXT NOT NULL UNIQUE,
            scenario_id TEXT NOT NULL,
            state TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scenario_onboarding_scenario_ts
            ON scenario_onboarding(scenario_id, ts DESC);
        CREATE TABLE IF NOT EXISTS live_shadow_comparison_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comparison_id TEXT NOT NULL UNIQUE,
            subject_key TEXT NOT NULL,
            revision_id TEXT NOT NULL DEFAULT '',
            window_start REAL NOT NULL DEFAULT 0,
            window_end REAL NOT NULL DEFAULT 0,
            baseline_json TEXT NOT NULL DEFAULT '{}',
            shadow_json TEXT NOT NULL DEFAULT '{}',
            live_json TEXT NOT NULL DEFAULT '{}',
            deltas_json TEXT NOT NULL DEFAULT '{}',
            payload_json TEXT NOT NULL DEFAULT '{}',
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_live_shadow_subject_ts
            ON live_shadow_comparison_records(subject_key, ts DESC);
        CREATE TABLE IF NOT EXISTS collaboration_rollout_subject_grants (
            subject_key TEXT PRIMARY KEY,
            actor TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            granted_at REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_outcome_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            goal_id TEXT NOT NULL DEFAULT '',
            scenario_id TEXT NOT NULL DEFAULT '',
            pack_id TEXT NOT NULL DEFAULT '',
            receipt_kind TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            proof_refs_json TEXT NOT NULL DEFAULT '{}',
            delivery_state TEXT NOT NULL DEFAULT '',
            next_step TEXT NOT NULL DEFAULT '',
            pass_hint INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            closure_state TEXT NOT NULL DEFAULT '',
            closure_proof_id TEXT NOT NULL DEFAULT '',
            followthrough_kind TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_user_outcome_receipts_task
            ON user_outcome_receipts(task_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_outcome_receipts_scenario
            ON user_outcome_receipts(scenario_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_outcome_receipts_pack
            ON user_outcome_receipts(pack_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS continuation_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            continuation_id TEXT NOT NULL UNIQUE,
            principal_id TEXT NOT NULL DEFAULT '',
            source_channel TEXT NOT NULL DEFAULT '',
            source_task_id TEXT NOT NULL DEFAULT '',
            linked_task_id TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            confidence_band TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_records_linked
            ON continuation_records(linked_task_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_continuation_records_principal
            ON continuation_records(principal_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS domain_repair_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_outcome_id TEXT NOT NULL UNIQUE,
            domain_id TEXT NOT NULL DEFAULT '',
            scenario_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            repair_family TEXT NOT NULL DEFAULT '',
            executed INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            fallback_used INTEGER NOT NULL DEFAULT 0,
            trust_safe INTEGER NOT NULL DEFAULT 1,
            ts REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_domain_repair_outcomes_task
            ON domain_repair_outcomes(task_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_domain_repair_outcomes_domain
            ON domain_repair_outcomes(domain_id, ts DESC);
        CREATE TABLE IF NOT EXISTS domain_rollout_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            pack_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_domain_rollout_decisions_pack
            ON domain_rollout_decisions(pack_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS open_loop_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL DEFAULT '',
            goal_id TEXT NOT NULL DEFAULT '',
            scenario_id TEXT NOT NULL DEFAULT '',
            pack_id TEXT NOT NULL DEFAULT '',
            loop_kind TEXT NOT NULL DEFAULT '',
            open_loop_state TEXT NOT NULL DEFAULT '',
            opened_reason TEXT NOT NULL DEFAULT '',
            opened_at REAL NOT NULL,
            due_at REAL NOT NULL DEFAULT 0,
            owner_kind TEXT NOT NULL DEFAULT '',
            receipt_id TEXT NOT NULL DEFAULT '',
            risk_tier TEXT NOT NULL DEFAULT 'low',
            proof_refs_json TEXT NOT NULL DEFAULT '{}',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_open_loop_task_opened
            ON open_loop_records(task_id, opened_at DESC);
        CREATE INDEX IF NOT EXISTS idx_open_loop_pack
            ON open_loop_records(pack_id, opened_at DESC);
        CREATE TABLE IF NOT EXISTS closure_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            closure_state TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            proof_kind TEXT NOT NULL DEFAULT '',
            proof_refs_json TEXT NOT NULL DEFAULT '{}',
            confidence_band TEXT NOT NULL DEFAULT '',
            actor_or_rule TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_closure_loop
            ON closure_decisions(loop_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_closure_task
            ON closure_decisions(task_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS continuation_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            trigger_type TEXT NOT NULL DEFAULT '',
            due_at REAL NOT NULL DEFAULT 0,
            eligibility TEXT NOT NULL DEFAULT '',
            evidence_snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_triggers_task
            ON continuation_triggers(task_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS followup_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            recommended_action TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            why_now TEXT NOT NULL DEFAULT '',
            urgency TEXT NOT NULL DEFAULT '',
            shadow_only INTEGER NOT NULL DEFAULT 1,
            risk_notes TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_followup_reco_task
            ON followup_recommendations(task_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS continuation_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            action_kind TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            executed INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            message_ref TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_exec_task
            ON continuation_executions(task_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS closure_proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proof_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            proof_kind TEXT NOT NULL DEFAULT '',
            proof_refs_json TEXT NOT NULL DEFAULT '{}',
            verdict TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_closure_proofs_loop
            ON closure_proofs(loop_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS stale_task_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            indicator_id TEXT NOT NULL UNIQUE,
            loop_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            staleness_kind TEXT NOT NULL DEFAULT '',
            window_seconds REAL NOT NULL DEFAULT 0,
            severity TEXT NOT NULL DEFAULT '',
            detected_at REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_stale_task_task
            ON stale_task_indicators(task_id, detected_at DESC);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()
    _apply_user_outcome_receipt_followthrough_columns(conn)


def _apply_user_outcome_receipt_followthrough_columns(conn: sqlite3.Connection) -> None:
    """Best-effort ALTER for existing DBs (SQLite has no IF NOT EXISTS for columns)."""
    for stmt in (
        "ALTER TABLE user_outcome_receipts ADD COLUMN closure_state TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE user_outcome_receipts ADD COLUMN closure_proof_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE user_outcome_receipts ADD COLUMN followthrough_kind TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass


def append_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: EventType,
    payload: Dict[str, Any],
) -> int:
    ts = time.time()
    cur = conn.execute(
        "INSERT INTO events(task_id, ts, event_type, payload_json) VALUES (?,?,?,?)",
        (task_id, ts, event_type.value, json.dumps(payload, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_collaboration_activation_decision(
    conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]
) -> None:
    ts = float(payload.get("recorded_at") or time.time())
    conn.execute(
        """
        INSERT INTO collaboration_activation_decisions(
            task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
            activation_mode, policy_version, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id,
            str(payload.get("plan_id") or ""),
            str(payload.get("step_id") or ""),
            str(payload.get("collab_id") or ""),
            str(payload.get("scenario_id") or ""),
            str(payload.get("trigger") or ""),
            ts,
            str(payload.get("activation_mode") or ""),
            str(payload.get("policy_version") or ""),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_collaboration_outcome_row(
    conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]
) -> None:
    ts = float(payload.get("recorded_at") or time.time())
    conn.execute(
        """
        INSERT INTO collaboration_outcomes(
            task_id, plan_id, step_id, collab_id, scenario_id, trigger, ts,
            canonical_class, usefulness_detail, live_advisory_ran, role_invocation_delta,
            payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id,
            str(payload.get("plan_id") or ""),
            str(payload.get("step_id") or ""),
            str(payload.get("collab_id") or ""),
            str(payload.get("scenario_id") or ""),
            str(payload.get("trigger") or ""),
            ts,
            str(payload.get("canonical_class") or ""),
            str(payload.get("usefulness_detail") or "")[:200],
            1 if payload.get("live_advisory_ran") else 0,
            int(payload.get("role_invocation_delta") or 0),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_repair_outcome_row(conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]) -> None:
    ts = float(payload.get("recorded_at") or time.time())
    conn.execute(
        """
        INSERT INTO repair_outcomes(
            task_id, plan_id, collab_id, ts, action_type, executed, payload_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            task_id,
            str(payload.get("plan_id") or ""),
            str(payload.get("collab_id") or ""),
            ts,
            str(payload.get("action_type") or "")[:120],
            1 if payload.get("executed") else 0,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def supersede_active_promotion_revisions(conn: sqlite3.Connection, subject_key: str) -> None:
    conn.execute(
        """
        UPDATE collaboration_promotion_revisions
        SET status = 'superseded'
        WHERE subject_key = ? AND status = 'active'
        """,
        (str(subject_key or ""),),
    )
    conn.commit()


def insert_collaboration_promotion_revision(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    subject_key: str,
    promotion_level: str,
    status: str = "active",
    operator_ack: int = 0,
    fallback_revision_id: str = "",
    evidence_snapshot: Optional[Dict[str, Any]] = None,
    risk_notes: str = "",
    payload: Optional[Dict[str, Any]] = None,
    created_at: Optional[float] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO collaboration_promotion_revisions(
            revision_id, subject_key, promotion_level, status, operator_ack,
            fallback_revision_id, evidence_snapshot_json, risk_notes, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(revision_id or ""),
            str(subject_key or ""),
            str(promotion_level or ""),
            str(status or "active"),
            1 if operator_ack else 0,
            str(fallback_revision_id or ""),
            json.dumps(evidence_snapshot or {}, ensure_ascii=False),
            str(risk_notes or "")[:2000],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def fetch_active_promotion_revision(
    conn: sqlite3.Connection, subject_key: str
) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT * FROM collaboration_promotion_revisions
            WHERE subject_key = ? AND status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(subject_key or ""),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def insert_collaboration_promotion_rollback(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    subject_key: str,
    trigger_type: str,
    observed: Optional[Dict[str, Any]] = None,
    fallback_revision_id: str = "",
    reason_codes: Optional[List[str]] = None,
    ts: Optional[float] = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO collaboration_promotion_rollbacks(
            revision_id, subject_key, trigger_type, observed_json,
            fallback_revision_id, reason_codes_json, ts
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            str(revision_id or ""),
            str(subject_key or ""),
            str(trigger_type or ""),
            json.dumps(observed or {}, ensure_ascii=False),
            str(fallback_revision_id or ""),
            json.dumps(list(reason_codes or []), ensure_ascii=False),
            t,
        ),
    )
    conn.commit()


def list_recent_promotion_rollbacks(
    conn: sqlite3.Connection, *, limit: int = 12
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM collaboration_promotion_rollbacks
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def list_active_promotion_revisions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM collaboration_promotion_revisions
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT 64
                """
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def insert_collaboration_operator_action(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    actor: str,
    action_kind: str,
    subject_key: str = "",
    scenario_id: str = "",
    trigger: str = "",
    requested_level: str = "",
    decision: str = "",
    revision_id: str = "",
    reason: str = "",
    payload: Optional[Dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO collaboration_operator_actions(
            action_id, actor, action_kind, subject_key, scenario_id, trigger,
            requested_level, decision, revision_id, reason, payload_json, ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(action_id or ""),
            str(actor or "")[:200],
            str(action_kind or "")[:120],
            str(subject_key or "")[:240],
            str(scenario_id or "")[:120],
            str(trigger or "")[:120],
            str(requested_level or "")[:80],
            str(decision or "")[:80],
            str(revision_id or "")[:120],
            str(reason or "")[:2000],
            json.dumps(payload or {}, ensure_ascii=False),
            t,
        ),
    )
    conn.commit()


def list_recent_operator_actions(
    conn: sqlite3.Connection, *, limit: int = 24
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM collaboration_operator_actions
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def insert_scenario_onboarding_record(
    conn: sqlite3.Connection,
    *,
    onboarding_id: str,
    scenario_id: str,
    state: str,
    actor: str = "",
    notes: str = "",
    evidence: Optional[Dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO scenario_onboarding(
            onboarding_id, scenario_id, state, actor, notes, evidence_json, ts
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            str(onboarding_id or ""),
            str(scenario_id or "")[:120],
            str(state or "")[:80],
            str(actor or "")[:200],
            str(notes or "")[:2000],
            json.dumps(evidence or {}, ensure_ascii=False),
            t,
        ),
    )
    conn.commit()


def fetch_latest_scenario_onboarding(
    conn: sqlite3.Connection, scenario_id: str
) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT * FROM scenario_onboarding
            WHERE scenario_id = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (str(scenario_id or ""),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def insert_live_shadow_comparison_record(
    conn: sqlite3.Connection,
    *,
    comparison_id: str,
    subject_key: str,
    revision_id: str = "",
    window_start: float = 0.0,
    window_end: float = 0.0,
    baseline: Optional[Dict[str, Any]] = None,
    shadow: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
    deltas: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO live_shadow_comparison_records(
            comparison_id, subject_key, revision_id, window_start, window_end,
            baseline_json, shadow_json, live_json, deltas_json, payload_json, ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(comparison_id or ""),
            str(subject_key or "")[:240],
            str(revision_id or "")[:120],
            float(window_start or 0.0),
            float(window_end or 0.0),
            json.dumps(baseline or {}, ensure_ascii=False),
            json.dumps(shadow or {}, ensure_ascii=False),
            json.dumps(live or {}, ensure_ascii=False),
            json.dumps(deltas or {}, ensure_ascii=False),
            json.dumps(payload or {}, ensure_ascii=False),
            t,
        ),
    )
    conn.commit()


def list_recent_live_shadow_comparisons(
    conn: sqlite3.Connection, *, limit: int = 16
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM live_shadow_comparison_records
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def upsert_rollout_subject_grant(
    conn: sqlite3.Connection,
    *,
    subject_key: str,
    actor: str = "",
    notes: str = "",
) -> None:
    ts = time.time()
    conn.execute(
        """
        INSERT INTO collaboration_rollout_subject_grants(
            subject_key, actor, notes, granted_at, revoked
        ) VALUES (?,?,?,?,0)
        ON CONFLICT(subject_key) DO UPDATE SET
            actor = excluded.actor,
            notes = excluded.notes,
            granted_at = excluded.granted_at,
            revoked = 0
        """,
        (
            str(subject_key or "")[:240],
            str(actor or "")[:200],
            str(notes or "")[:2000],
            ts,
        ),
    )
    conn.commit()


def revoke_rollout_subject_grant(conn: sqlite3.Connection, subject_key: str) -> None:
    conn.execute(
        """
        UPDATE collaboration_rollout_subject_grants
        SET revoked = 1
        WHERE subject_key = ?
        """,
        (str(subject_key or ""),),
    )
    conn.commit()


def list_active_rollout_subject_grants(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM collaboration_rollout_subject_grants
                WHERE revoked = 0
                ORDER BY granted_at DESC
                LIMIT 64
                """
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def subject_has_active_rollout_grant(conn: sqlite3.Connection, subject_key: str) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1 FROM collaboration_rollout_subject_grants
            WHERE subject_key = ? AND revoked = 0
            LIMIT 1
            """,
            (str(subject_key or ""),),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def insert_user_outcome_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    task_id: str,
    goal_id: str = "",
    scenario_id: str = "",
    pack_id: str = "",
    receipt_kind: str = "",
    summary: str = "",
    proof_refs: Optional[Dict[str, Any]] = None,
    delivery_state: str = "",
    next_step: str = "",
    pass_hint: bool = True,
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
    closure_state: str = "",
    closure_proof_id: str = "",
    followthrough_kind: str = "",
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO user_outcome_receipts(
            receipt_id, task_id, goal_id, scenario_id, pack_id, receipt_kind, summary,
            proof_refs_json, delivery_state, next_step, pass_hint, created_at, payload_json,
            closure_state, closure_proof_id, followthrough_kind
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(receipt_id or ""),
            str(task_id or ""),
            str(goal_id or ""),
            str(scenario_id or "")[:120],
            str(pack_id or "")[:120],
            str(receipt_kind or "")[:120],
            str(summary or "")[:4000],
            json.dumps(proof_refs or {}, ensure_ascii=False),
            str(delivery_state or "")[:120],
            str(next_step or "")[:2000],
            1 if pass_hint else 0,
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
            str(closure_state or "")[:80],
            str(closure_proof_id or "")[:120],
            str(followthrough_kind or "")[:120],
        ),
    )
    conn.commit()


def update_user_outcome_receipt_followthrough(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    closure_state: str = "",
    closure_proof_id: str = "",
    followthrough_kind: str = "",
) -> None:
    try:
        conn.execute(
            """
            UPDATE user_outcome_receipts
            SET closure_state = ?, closure_proof_id = ?, followthrough_kind = ?
            WHERE receipt_id = ?
            """,
            (
                str(closure_state or "")[:80],
                str(closure_proof_id or "")[:120],
                str(followthrough_kind or "")[:120],
                str(receipt_id or ""),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def list_recent_user_outcome_receipts(
    conn: sqlite3.Connection,
    *,
    pack_id: str = "",
    since_ts: float = 0.0,
    limit: int = 64,
) -> List[sqlite3.Row]:
    try:
        lim = max(1, int(limit))
        if str(pack_id or "").strip():
            return list(
                conn.execute(
                    """
                    SELECT * FROM user_outcome_receipts
                    WHERE pack_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(pack_id), float(since_ts or 0.0), lim),
                ).fetchall()
            )
        return list(
            conn.execute(
                """
                SELECT * FROM user_outcome_receipts
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (float(since_ts or 0.0), lim),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def count_user_outcome_receipts_window(
    conn: sqlite3.Connection,
    *,
    pack_id: str,
    since_ts: float,
) -> Tuple[int, int]:
    """Returns (total, pass_count) for receipt pass-rate heuristics."""
    try:
        rows = conn.execute(
            """
            SELECT pass_hint FROM user_outcome_receipts
            WHERE pack_id = ? AND created_at >= ?
            """,
            (str(pack_id or ""), float(since_ts or 0.0)),
        ).fetchall()
        total = len(rows)
        passed = sum(1 for r in rows if int(r["pass_hint"] or 0) == 1)
        return total, passed
    except sqlite3.OperationalError:
        return 0, 0


def daily_pack_proving_window_aggregates(
    conn: sqlite3.Connection,
    *,
    pack_id: str,
    scenario_ids: Tuple[str, ...],
    since_ts: float,
) -> Dict[str, Any]:
    """
    Pack-scoped ledgers for daily proving: routed tasks (ScenarioResolved),
    receipt coverage/quality/failure rows, continuations, domain repairs.

    All windows use the same since_ts floor; callers typically use a 7d horizon.
    """
    pid = str(pack_id or "")
    since = float(since_ts or 0.0)
    sids = tuple(str(s) for s in scenario_ids if str(s).strip())
    out: Dict[str, Any] = {
        "routed_distinct_task_count": 0,
        "ingress_breakdown": {},
        "receipt_row_count": 0,
        "receipt_distinct_task_count": 0,
        "receipt_pass_count": 0,
        "receipt_quality_good_count": 0,
        "receipt_completed_closure_count": 0,
        "receipt_needs_repair_count": 0,
        "continuation_count": 0,
        "domain_repair_count": 0,
    }
    if not sids:
        return out

    try:
        ph = ",".join("?" * len(sids))
        q_routed = f"""
            SELECT DISTINCT e.task_id AS task_id, IFNULL(t.channel, '') AS channel
            FROM events e
            INNER JOIN tasks t ON t.task_id = e.task_id
            WHERE e.event_type = ?
              AND e.ts >= ?
              AND json_extract(e.payload_json, '$.scenario_id') IN ({ph})
        """
        params_routed: List[Any] = [EventType.SCENARIO_RESOLVED.value, since, *sids]
        rrows = conn.execute(q_routed, params_routed).fetchall()
        channels: Dict[str, int] = {}
        for r in rrows:
            tid = str(r["task_id"] or "")
            if not tid:
                continue
            ch = str(r["channel"] or "") or "unknown"
            channels[ch] = channels.get(ch, 0) + 1
        # DISTINCT task_ids: one row per task in rrows may duplicate if join weird — query uses DISTINCT
        out["routed_distinct_task_count"] = len(rrows)
        out["ingress_breakdown"] = dict(sorted(channels.items()))
    except sqlite3.OperationalError:
        pass

    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS receipt_row_count,
              COUNT(DISTINCT task_id) AS receipt_distinct_task_count,
              SUM(CASE WHEN pass_hint = 1 THEN 1 ELSE 0 END) AS receipt_pass_count,
              SUM(
                CASE
                  WHEN pass_hint = 1 AND IFNULL(closure_state, '') != 'needs_repair' THEN 1
                  ELSE 0
                END
              ) AS receipt_quality_good_count,
              SUM(CASE WHEN closure_state = 'completed' THEN 1 ELSE 0 END) AS receipt_completed_closure_count,
              SUM(CASE WHEN closure_state = 'needs_repair' THEN 1 ELSE 0 END) AS receipt_needs_repair_count
            FROM user_outcome_receipts
            WHERE pack_id = ? AND created_at >= ?
            """,
            (pid, since),
        ).fetchone()
        if row:
            out["receipt_row_count"] = int(row["receipt_row_count"] or 0)
            out["receipt_distinct_task_count"] = int(row["receipt_distinct_task_count"] or 0)
            out["receipt_pass_count"] = int(row["receipt_pass_count"] or 0)
            out["receipt_quality_good_count"] = int(row["receipt_quality_good_count"] or 0)
            out["receipt_completed_closure_count"] = int(row["receipt_completed_closure_count"] or 0)
            out["receipt_needs_repair_count"] = int(row["receipt_needs_repair_count"] or 0)
    except sqlite3.OperationalError:
        pass

    try:
        c_row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM continuation_records WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        out["continuation_count"] = int(c_row["c"] or 0) if c_row else 0
    except sqlite3.OperationalError:
        pass

    try:
        ph2 = ",".join("?" * len(sids))
        dr_row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM domain_repair_outcomes
            WHERE ts >= ? AND scenario_id IN ({ph2})
            """,
            (since, *sids),
        ).fetchone()
        out["domain_repair_count"] = int(dr_row["c"] or 0) if dr_row else 0
    except sqlite3.OperationalError:
        pass

    return out


def insert_continuation_record(
    conn: sqlite3.Connection,
    *,
    continuation_id: str,
    principal_id: str = "",
    source_channel: str = "",
    source_task_id: str = "",
    linked_task_id: str = "",
    reason: str = "",
    confidence_band: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO continuation_records(
            continuation_id, principal_id, source_channel, source_task_id, linked_task_id,
            reason, confidence_band, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            str(continuation_id or ""),
            str(principal_id or ""),
            str(source_channel or "")[:40],
            str(source_task_id or ""),
            str(linked_task_id or ""),
            str(reason or "")[:500],
            str(confidence_band or "")[:80],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def list_recent_continuation_records(
    conn: sqlite3.Connection, *, limit: int = 32
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM continuation_records
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def insert_domain_repair_outcome_row(
    conn: sqlite3.Connection,
    *,
    repair_outcome_id: str,
    domain_id: str = "",
    scenario_id: str = "",
    task_id: str = "",
    repair_family: str = "",
    executed: bool = False,
    result: str = "",
    fallback_used: bool = False,
    trust_safe: bool = True,
    ts: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    t = float(ts if ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO domain_repair_outcomes(
            repair_outcome_id, domain_id, scenario_id, task_id, repair_family,
            executed, result, fallback_used, trust_safe, ts, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(repair_outcome_id or ""),
            str(domain_id or "")[:120],
            str(scenario_id or "")[:120],
            str(task_id or ""),
            str(repair_family or "")[:120],
            1 if executed else 0,
            str(result or "")[:2000],
            1 if fallback_used else 0,
            1 if trust_safe else 0,
            t,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def list_recent_domain_repair_outcomes(
    conn: sqlite3.Connection, *, limit: int = 24
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM domain_repair_outcomes
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def insert_domain_rollout_decision_row(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    pack_id: str = "",
    decision: str = "",
    actor: str = "",
    reason: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO domain_rollout_decisions(
            decision_id, pack_id, decision, actor, reason, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            str(decision_id or ""),
            str(pack_id or "")[:120],
            str(decision or "")[:80],
            str(actor or "")[:200],
            str(reason or "")[:2000],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def list_recent_domain_rollout_decisions(
    conn: sqlite3.Connection, *, pack_id: str = "", limit: int = 16
) -> List[sqlite3.Row]:
    try:
        lim = max(1, int(limit))
        if str(pack_id or "").strip():
            return list(
                conn.execute(
                    """
                    SELECT * FROM domain_rollout_decisions
                    WHERE pack_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(pack_id), lim),
                ).fetchall()
            )
        return list(
            conn.execute(
                """
                SELECT * FROM domain_rollout_decisions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def insert_open_loop_record(
    conn: sqlite3.Connection,
    *,
    loop_id: str,
    task_id: str,
    goal_id: str = "",
    scenario_id: str = "",
    pack_id: str = "",
    loop_kind: str = "",
    open_loop_state: str = "",
    opened_reason: str = "",
    opened_at: Optional[float] = None,
    due_at: float = 0.0,
    owner_kind: str = "user",
    receipt_id: str = "",
    risk_tier: str = "low",
    proof_refs: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(opened_at if opened_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO open_loop_records(
            loop_id, task_id, goal_id, scenario_id, pack_id, loop_kind, open_loop_state,
            opened_reason, opened_at, due_at, owner_kind, receipt_id, risk_tier,
            proof_refs_json, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(loop_id or ""),
            str(task_id or ""),
            str(goal_id or ""),
            str(scenario_id or "")[:120],
            str(pack_id or "")[:120],
            str(loop_kind or "")[:120],
            str(open_loop_state or "")[:80],
            str(opened_reason or "")[:2000],
            ts,
            float(due_at or 0.0),
            str(owner_kind or "")[:40],
            str(receipt_id or "")[:120],
            str(risk_tier or "")[:40],
            json.dumps(proof_refs or {}, ensure_ascii=False),
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_closure_decision_row(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    loop_id: str = "",
    task_id: str = "",
    closure_state: str = "",
    reason: str = "",
    proof_kind: str = "",
    proof_refs: Optional[Dict[str, Any]] = None,
    confidence_band: str = "",
    actor_or_rule: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO closure_decisions(
            decision_id, loop_id, task_id, closure_state, reason, proof_kind,
            proof_refs_json, confidence_band, actor_or_rule, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(decision_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(closure_state or "")[:80],
            str(reason or "")[:2000],
            str(proof_kind or "")[:120],
            json.dumps(proof_refs or {}, ensure_ascii=False),
            str(confidence_band or "")[:80],
            str(actor_or_rule or "")[:120],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_closure_proof_row(
    conn: sqlite3.Connection,
    *,
    proof_id: str,
    loop_id: str = "",
    task_id: str = "",
    proof_kind: str = "",
    proof_refs: Optional[Dict[str, Any]] = None,
    verdict: str = "",
    summary: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO closure_proofs(
            proof_id, loop_id, task_id, proof_kind, proof_refs_json, verdict, summary,
            created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            str(proof_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(proof_kind or "")[:120],
            json.dumps(proof_refs or {}, ensure_ascii=False),
            str(verdict or "")[:80],
            str(summary or "")[:2000],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_continuation_trigger_row(
    conn: sqlite3.Connection,
    *,
    trigger_id: str,
    loop_id: str = "",
    task_id: str = "",
    trigger_type: str = "",
    due_at: float = 0.0,
    eligibility: str = "",
    evidence_snapshot: Optional[Dict[str, Any]] = None,
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO continuation_triggers(
            trigger_id, loop_id, task_id, trigger_type, due_at, eligibility,
            evidence_snapshot_json, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            str(trigger_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(trigger_type or "")[:120],
            float(due_at or 0.0),
            str(eligibility or "")[:120],
            json.dumps(evidence_snapshot or {}, ensure_ascii=False),
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_followup_recommendation_row(
    conn: sqlite3.Connection,
    *,
    recommendation_id: str,
    loop_id: str = "",
    task_id: str = "",
    recommended_action: str = "",
    channel: str = "",
    why_now: str = "",
    urgency: str = "",
    shadow_only: bool = True,
    risk_notes: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO followup_recommendations(
            recommendation_id, loop_id, task_id, recommended_action, channel, why_now,
            urgency, shadow_only, risk_notes, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(recommendation_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(recommended_action or "")[:120],
            str(channel or "")[:80],
            str(why_now or "")[:2000],
            str(urgency or "")[:40],
            1 if shadow_only else 0,
            str(risk_notes or "")[:2000],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_continuation_execution_row(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    loop_id: str = "",
    task_id: str = "",
    action_kind: str = "",
    channel: str = "",
    executed: bool = False,
    result: str = "",
    message_ref: str = "",
    created_at: Optional[float] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(created_at if created_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO continuation_executions(
            execution_id, loop_id, task_id, action_kind, channel, executed, result,
            message_ref, created_at, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(execution_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(action_kind or "")[:120],
            str(channel or "")[:80],
            1 if executed else 0,
            str(result or "")[:2000],
            str(message_ref or "")[:500],
            ts,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def insert_stale_task_indicator_row(
    conn: sqlite3.Connection,
    *,
    indicator_id: str,
    loop_id: str = "",
    task_id: str = "",
    staleness_kind: str = "",
    window_seconds: float = 0.0,
    severity: str = "",
    detected_at: Optional[float] = None,
    reason: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    ts = float(detected_at if detected_at is not None else time.time())
    conn.execute(
        """
        INSERT INTO stale_task_indicators(
            indicator_id, loop_id, task_id, staleness_kind, window_seconds, severity,
            detected_at, reason, payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            str(indicator_id or ""),
            str(loop_id or ""),
            str(task_id or ""),
            str(staleness_kind or "")[:120],
            float(window_seconds or 0.0),
            str(severity or "")[:40],
            ts,
            str(reason or "")[:2000],
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def list_recent_open_loop_records(
    conn: sqlite3.Connection, *, pack_id: str = "", limit: int = 48
) -> List[sqlite3.Row]:
    try:
        lim = max(1, int(limit))
        if str(pack_id or "").strip():
            return list(
                conn.execute(
                    """
                    SELECT * FROM open_loop_records
                    WHERE pack_id = ?
                    ORDER BY opened_at DESC
                    LIMIT ?
                    """,
                    (str(pack_id), lim),
                ).fetchall()
            )
        return list(
            conn.execute(
                """
                SELECT * FROM open_loop_records
                ORDER BY opened_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def list_recent_closure_decisions(
    conn: sqlite3.Connection, *, limit: int = 48
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM closure_decisions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def list_recent_followup_recommendations(
    conn: sqlite3.Connection, *, limit: int = 32
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM followup_recommendations
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def list_recent_stale_task_indicators(
    conn: sqlite3.Connection, *, limit: int = 24
) -> List[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM stale_task_indicators
                ORDER BY detected_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def count_closure_decisions_window(
    conn: sqlite3.Connection,
    *,
    since_ts: float,
    closure_state: str = "",
) -> int:
    try:
        if str(closure_state or "").strip():
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM closure_decisions
                WHERE created_at >= ? AND closure_state = ?
                """,
                (float(since_ts or 0.0), str(closure_state)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM closure_decisions
                WHERE created_at >= ?
                """,
                (float(since_ts or 0.0),),
            ).fetchone()
        return int(row["c"] or 0) if row else 0
    except sqlite3.OperationalError:
        return 0


def count_open_loops_window(conn: sqlite3.Connection, *, since_ts: float) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM open_loop_records
            WHERE opened_at >= ?
            """,
            (float(since_ts or 0.0),),
        ).fetchone()
        return int(row["c"] or 0) if row else 0
    except sqlite3.OperationalError:
        return 0


def create_task(conn: sqlite3.Connection, task_id: str, channel: str) -> None:
    ts = time.time()
    conn.execute(
        "INSERT INTO tasks(task_id, channel, created_at, updated_at) VALUES (?,?,?,?)",
        (task_id, channel, ts, ts),
    )
    conn.commit()


def claim_idempotency_or_get_existing(
    conn: sqlite3.Connection, key: str, new_task_id: str
) -> Tuple[str, bool]:
    """
    Returns (task_id, is_new_claim).
    If key already mapped, returns existing task_id and is_new_claim False.
    Otherwise inserts mapping for new_task_id and returns (new_task_id, True).
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        return str(row["task_id"]), False
    ts = time.time()
    conn.execute(
        "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
        (key, new_task_id, ts),
    )
    conn.commit()
    return new_task_id, True


def delete_meta(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


def claim_scoped_idempotency(
    conn: sqlite3.Connection, key: str, task_id: str
) -> str:
    """
    Idempotency for operations on an existing task (Telegram retries, cursor reports).

    Returns:
        'fresh' — first time this key was claimed for this task_id
        'duplicate' — same key and same task_id (safe retry)
        'conflict' — key exists for a different task_id (should not happen)
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        existing = str(row["task_id"])
        if existing == task_id:
            return "duplicate"
        return "conflict"
    ts = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row2 = conn.execute(
            "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row2:
            conn.rollback()
            existing = str(row2["task_id"])
            if existing == task_id:
                return "duplicate"
            return "conflict"
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
            (key, task_id, ts),
        )
        conn.commit()
        return "fresh"
    except Exception:
        conn.rollback()
        raise


def claim_idempotency_and_create_task(
    conn: sqlite3.Connection,
    key: str,
    new_task_id: str,
    channel: str,
) -> Tuple[str, bool]:
    """
    Returns (task_id, created_new_task).

    This keeps the idempotency claim and task creation in one transaction so a
    crash cannot leave an idempotency mapping without a task row.
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        return str(row["task_id"]), False
    ts = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row:
            conn.rollback()
            return str(row["task_id"]), False
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
            (key, new_task_id, ts),
        )
        conn.execute(
            "INSERT INTO tasks(task_id, channel, created_at, updated_at) VALUES (?,?,?,?)",
            (new_task_id, channel, ts, ts),
        )
        conn.commit()
        return new_task_id, True
    except Exception:
        conn.rollback()
        raise


def load_events_for_task(
    conn: sqlite3.Connection, task_id: str
) -> List[Tuple[int, float, str, Dict[str, Any]]]:
    rows = conn.execute(
        "SELECT seq, ts, event_type, payload_json FROM events WHERE task_id = ? ORDER BY seq ASC",
        (task_id,),
    ).fetchall()
    out: List[Tuple[int, float, str, Dict[str, Any]]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            if os.environ.get("ANDREA_SYNC_JSON_PARSE_WARNINGS", "0") == "1":
                print(
                    f"andrea_sync JSON parse warning: task={task_id} seq={r['seq']}",
                    flush=True,
                )
            payload = {}
        try:
            seq = int(r["seq"])
            ts = float(r["ts"])
        except (TypeError, ValueError):
            continue
        out.append((seq, ts, str(r["event_type"]), payload))
    return out


def _clip_text(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def new_principal_id() -> str:
    return f"prn_{uuid.uuid4().hex[:16]}"


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:16]}"


def new_reminder_id() -> str:
    return f"rem_{uuid.uuid4().hex[:16]}"


def new_incident_id() -> str:
    return f"inc_{uuid.uuid4().hex[:16]}"


def new_repair_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:16]}"


def new_repair_plan_id() -> str:
    return f"rpl_{uuid.uuid4().hex[:16]}"


def principal_exists(conn: sqlite3.Connection, principal_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM principals WHERE principal_id = ?",
        (str(principal_id).strip(),),
    ).fetchone()
    return row is not None


def _principal_external_keys(channel: str, payload: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    ch = str(channel or "").strip().lower()
    if ch == Channel.TELEGRAM.value:
        if payload.get("chat_id") is not None:
            keys.append(f"chat:{payload.get('chat_id')}")
        if payload.get("from_user") is not None:
            keys.append(f"user:{payload.get('from_user')}")
        username = str(payload.get("from_username") or "").strip().lower()
        if username:
            keys.append(f"username:{username}")
    elif ch == Channel.ALEXA.value:
        user_id = str(payload.get("user_id") or "").strip()
        device_id = str(payload.get("device_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if user_id:
            keys.append(f"user:{user_id}")
        if device_id:
            keys.append(f"device:{device_id}")
        if session_id:
            keys.append(f"session:{session_id}")
    else:
        principal_key = str(payload.get("principal_key") or "").strip()
        if principal_key:
            keys.append(principal_key)
    deduped: List[str] = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def link_principal_identity(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    channel: str,
    external_key: str,
) -> None:
    pid = str(principal_id or "").strip()
    ch = str(channel or "").strip().lower()
    key = str(external_key or "").strip()
    if not pid or not ch or not key:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_links(channel, external_key, principal_id, created_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(channel, external_key) DO UPDATE SET
            principal_id = excluded.principal_id,
            updated_at = excluded.updated_at
        """,
        (ch, key, pid, ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def resolve_principal_id(
    conn: sqlite3.Connection,
    *,
    channel: str,
    payload: Dict[str, Any],
    principal_id_hint: str = "",
) -> str:
    hinted = str(principal_id_hint or payload.get("principal_id") or "").strip()
    if hinted and principal_exists(conn, hinted):
        return hinted
    ch = str(channel or "").strip().lower()
    candidate_ids: List[str] = []
    keys = _principal_external_keys(ch, payload)
    for key in keys:
        row = conn.execute(
            "SELECT principal_id FROM principal_links WHERE channel = ? AND external_key = ?",
            (ch, key),
        ).fetchone()
        if row:
            pid = str(row["principal_id"])
            if pid and pid not in candidate_ids:
                candidate_ids.append(pid)
    principal_id = candidate_ids[0] if candidate_ids else new_principal_id()
    ts = time.time()
    if not principal_exists(conn, principal_id):
        conn.execute(
            "INSERT INTO principals(principal_id, created_at, updated_at, display_name) VALUES (?,?,?,?)",
            (principal_id, ts, ts, ""),
        )
    for key in keys:
        conn.execute(
            """
            INSERT INTO principal_links(channel, external_key, principal_id, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(channel, external_key) DO UPDATE SET
                principal_id = excluded.principal_id,
                updated_at = excluded.updated_at
            """,
            (ch, key, principal_id, ts, ts),
        )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, principal_id),
    )
    conn.commit()
    return principal_id


def link_task_principal(
    conn: sqlite3.Connection,
    task_id: str,
    principal_id: str,
    *,
    channel: str,
) -> None:
    tid = str(task_id or "").strip()
    pid = str(principal_id or "").strip()
    ch = str(channel or "").strip().lower()
    if not tid or not pid or not ch:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO task_principals(task_id, principal_id, channel, created_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET
            principal_id = excluded.principal_id,
            channel = excluded.channel,
            updated_at = excluded.updated_at
        """,
        (tid, pid, ch, ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def get_task_principal_id(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT principal_id FROM task_principals WHERE task_id = ?",
        (str(task_id or "").strip(),),
    ).fetchone()
    return str(row["principal_id"]) if row else None


def save_principal_memory(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    content: str,
    kind: str = "note",
    source: str = "",
    source_task_id: str = "",
    memory_id: str = "",
) -> str:
    pid = str(principal_id or "").strip()
    text = str(content or "").strip()
    if not pid or not text:
        raise ValueError("principal_id and content are required")
    mid = str(memory_id or "").strip() or new_memory_id()
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_memories(memory_id, principal_id, kind, content, source, source_task_id, created_at, updated_at, is_active)
        VALUES (?,?,?,?,?,?,?,?,1)
        ON CONFLICT(memory_id) DO UPDATE SET
            kind = excluded.kind,
            content = excluded.content,
            source = excluded.source,
            source_task_id = excluded.source_task_id,
            updated_at = excluded.updated_at,
            is_active = 1
        """,
        (mid, pid, str(kind or "note"), text, str(source or ""), str(source_task_id or ""), ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()
    return mid


def list_principal_memories(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT memory_id, principal_id, kind, content, source, source_task_id, created_at, updated_at
        FROM principal_memories
        WHERE principal_id = ? AND is_active = 1
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(principal_id or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [dict(r) for r in rows]


def set_principal_preference(
    conn: sqlite3.Connection,
    principal_id: str,
    key: str,
    value: Any,
) -> None:
    pid = str(principal_id or "").strip()
    pref_key = str(key or "").strip()
    if not pid or not pref_key:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_preferences(principal_id, pref_key, pref_value_json, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(principal_id, pref_key) DO UPDATE SET
            pref_value_json = excluded.pref_value_json,
            updated_at = excluded.updated_at
        """,
        (pid, pref_key, json.dumps(value, ensure_ascii=False), ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def get_principal_preferences(conn: sqlite3.Connection, principal_id: str) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT pref_key, pref_value_json
        FROM principal_preferences
        WHERE principal_id = ?
        """,
        (str(principal_id or "").strip(),),
    ).fetchall()
    prefs: Dict[str, Any] = {}
    for row in rows:
        key = str(row["pref_key"] or "").strip()
        raw = row["pref_value_json"]
        if not key:
            continue
        try:
            prefs[key] = json.loads(raw)
        except Exception:
            prefs[key] = raw
    return prefs


def load_recent_principal_history(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    limit_turns: int = 8,
    exclude_task_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    pid = str(principal_id or "").strip()
    if not pid:
        return []
    params: List[Any] = [pid]
    exclude_clause = ""
    if exclude_task_id:
        exclude_clause = "AND tp.task_id != ?"
        params.append(str(exclude_task_id))
    params.append(max(1, int(limit_turns)))
    rows = conn.execute(
        f"""
        SELECT tp.task_id, t.channel, t.updated_at
        FROM task_principals tp
        JOIN tasks t ON t.task_id = tp.task_id
        WHERE tp.principal_id = ?
          {exclude_clause}
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    ordered = list(reversed(rows))
    history: List[Dict[str, str]] = []
    for row in ordered:
        task_id = str(row["task_id"])
        channel = str(row["channel"] or "")
        user_text = ""
        assistant_text = ""
        assistant_source = ""
        for _seq, _ts, et_raw, payload in load_events_for_task(conn, task_id):
            if et_raw == EventType.USER_MESSAGE.value and payload.get("text"):
                user_text = _clip_text(payload.get("routing_text") or payload.get("text"))
            elif et_raw == EventType.ASSISTANT_REPLIED.value and payload.get("text"):
                assistant_text = _clip_text(payload.get("text"))
                assistant_source = "direct"
            elif et_raw == EventType.JOB_COMPLETED.value and payload.get("summary"):
                backend = str(payload.get("backend") or "").strip()
                delegated = bool(payload.get("delegated_to_cursor"))
                if backend == "openclaw" and delegated:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw_cursor"
                elif backend == "openclaw":
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw"
                else:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "cursor"
            elif et_raw == EventType.JOB_FAILED.value:
                detail = payload.get("user_safe_error") or payload.get("message") or payload.get("error")
                if detail:
                    backend = str(payload.get("backend") or "").strip()
                    if backend == "openclaw":
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "openclaw"
                    else:
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "cursor"
        if user_text:
            history.append(
                {
                    "role": "user",
                    "content": user_text,
                    "task_id": task_id,
                    "channel": channel,
                }
            )
        if assistant_text:
            history.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "task_id": task_id,
                    "channel": channel,
                    "source": assistant_source or "direct",
                }
            )
    return history


def get_principal_recent_telegram_chat_id(
    conn: sqlite3.Connection, principal_id: str
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) AS chat_id
        FROM task_principals tp
        JOIN tasks t ON t.task_id = tp.task_id
        JOIN events e ON e.task_id = tp.task_id
        WHERE tp.principal_id = ?
          AND t.channel = ?
          AND e.event_type = ?
          AND json_extract(e.payload_json, '$.chat_id') IS NOT NULL
        ORDER BY e.seq DESC
        LIMIT 1
        """,
        (
            str(principal_id or "").strip(),
            Channel.TELEGRAM.value,
            EventType.USER_MESSAGE.value,
        ),
    ).fetchone()
    if not row:
        return None
    value = str(row["chat_id"] or "").strip()
    return value or None


def create_reminder(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    channel: str,
    delivery_target: str,
    message: str,
    due_at: float,
    status: str = "scheduled",
    source_task_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    reminder_id: str = "",
) -> str:
    pid = str(principal_id or "").strip()
    text = str(message or "").strip()
    rid = str(reminder_id or "").strip() or new_reminder_id()
    if not pid or not text:
        raise ValueError("principal_id and message are required")
    ts = time.time()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO reminders(
            reminder_id, principal_id, channel, delivery_target, message, due_at,
            status, source_task_id, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(reminder_id) DO UPDATE SET
            channel = excluded.channel,
            delivery_target = excluded.delivery_target,
            message = excluded.message,
            due_at = excluded.due_at,
            status = excluded.status,
            source_task_id = excluded.source_task_id,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            rid,
            pid,
            str(channel or "").strip().lower(),
            str(delivery_target or "").strip(),
            text,
            float(due_at),
            str(status or "scheduled"),
            str(source_task_id or "").strip(),
            metadata_json,
            ts,
            ts,
        ),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()
    return rid


def list_due_reminders(
    conn: sqlite3.Connection,
    *,
    now_ts: Optional[float] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    ts = float(now_ts or time.time())
    rows = conn.execute(
        """
        SELECT reminder_id, principal_id, channel, delivery_target, message, due_at, status,
               source_task_id, metadata_json, created_at, updated_at
        FROM reminders
        WHERE status IN ('scheduled', 'awaiting_delivery_channel')
          AND due_at <= ?
        ORDER BY due_at ASC, created_at ASC
        LIMIT ?
        """,
        (ts, max(1, int(limit))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_meta = item.get("metadata_json")
        try:
            item["metadata"] = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            item["metadata"] = {}
        out.append(item)
    return out


def update_reminder(
    conn: sqlite3.Connection,
    reminder_id: str,
    *,
    status: Optional[str] = None,
    delivery_target: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    rid = str(reminder_id or "").strip()
    if not rid:
        return
    row = conn.execute(
        "SELECT metadata_json FROM reminders WHERE reminder_id = ?",
        (rid,),
    ).fetchone()
    if not row:
        return
    try:
        merged = json.loads(row["metadata_json"] or "{}")
    except Exception:
        merged = {}
    if metadata:
        merged.update(metadata)
    ts = time.time()
    conn.execute(
        """
        UPDATE reminders
        SET status = COALESCE(?, status),
            delivery_target = COALESCE(?, delivery_target),
            metadata_json = ?,
            updated_at = ?
        WHERE reminder_id = ?
        """,
        (
            str(status).strip() if status is not None else None,
            str(delivery_target).strip() if delivery_target is not None else None,
            json.dumps(merged, ensure_ascii=False),
            ts,
            rid,
        ),
    )
    conn.commit()


def save_incident(conn: sqlite3.Connection, incident: Dict[str, Any]) -> str:
    payload = dict(incident)
    incident_id = str(payload.get("incident_id") or "").strip() or new_incident_id()
    ts = time.time()
    created_at = float(payload.get("timestamp") or payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO incidents(
            incident_id, source_task_id, source, error_type, status, summary, fingerprint,
            created_at, updated_at, incident_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(incident_id) DO UPDATE SET
            source_task_id = excluded.source_task_id,
            source = excluded.source,
            error_type = excluded.error_type,
            status = excluded.status,
            summary = excluded.summary,
            fingerprint = excluded.fingerprint,
            updated_at = excluded.updated_at,
            incident_json = excluded.incident_json
        """,
        (
            incident_id,
            str(payload.get("source_task_id") or "").strip(),
            str(payload.get("source") or "unknown").strip(),
            str(payload.get("error_type") or "unclear_or_unsafe").strip(),
            str(payload.get("status") or "open").strip(),
            _clip_text(payload.get("summary") or "", 500),
            str(payload.get("fingerprint") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "incident_id": incident_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return incident_id


def get_incident(conn: sqlite3.Connection, incident_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT incident_id, source_task_id, source, error_type, status, summary, fingerprint,
               created_at, updated_at, incident_json
        FROM incidents
        WHERE incident_id = ?
        """,
        (str(incident_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    payload: Dict[str, Any]
    try:
        payload = json.loads(row["incident_json"] or "{}")
    except Exception:
        payload = {}
    return {
        **payload,
        "incident_id": str(row["incident_id"] or ""),
        "source_task_id": str(row["source_task_id"] or ""),
        "source": str(row["source"] or ""),
        "error_type": str(row["error_type"] or ""),
        "status": str(row["status"] or ""),
        "summary": str(row["summary"] or ""),
        "fingerprint": str(row["fingerprint"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
    }


def list_incidents(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT incident_id FROM incidents
        WHERE (? = '' OR status = ?)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(status or "").strip(), str(status or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [get_incident(conn, str(row["incident_id"])) for row in rows]


def save_experience_run(conn: sqlite3.Connection, run: Dict[str, Any]) -> str:
    payload = dict(run)
    run_id = str(payload.get("run_id") or "").strip() or new_experience_run_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or payload.get("started_at") or ts)
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    conn.execute(
        """
        INSERT INTO experience_runs(
            run_id, actor, status, passed, summary, total_checks, failed_checks,
            average_score, created_at, updated_at, run_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
            actor = excluded.actor,
            status = excluded.status,
            passed = excluded.passed,
            summary = excluded.summary,
            total_checks = excluded.total_checks,
            failed_checks = excluded.failed_checks,
            average_score = excluded.average_score,
            updated_at = excluded.updated_at,
            run_json = excluded.run_json
        """,
        (
            run_id,
            str(payload.get("actor") or "").strip(),
            str(payload.get("status") or "completed").strip(),
            1 if bool(payload.get("passed")) else 0,
            str(payload.get("summary") or "").strip(),
            int(payload.get("total_checks") or len(checks)),
            int(payload.get("failed_checks") or 0),
            float(payload.get("average_score") or 0.0),
            created_at,
            ts,
            json.dumps({**payload, "run_id": run_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.execute("DELETE FROM experience_checks WHERE run_id = ?", (run_id,))
    for idx, raw in enumerate(checks):
        row = dict(raw) if isinstance(raw, dict) else {"summary": str(raw)}
        scenario_id = str(row.get("scenario_id") or row.get("check_id") or f"scenario_{idx+1}").strip()
        check_key = f"{run_id}:{scenario_id or idx + 1}"
        row_created_at = float(row.get("started_at") or payload.get("started_at") or created_at)
        conn.execute(
            """
            INSERT INTO experience_checks(
                check_key, run_id, scenario_id, status, score, created_at, updated_at, check_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                check_key,
                run_id,
                scenario_id,
                "passed" if bool(row.get("passed")) else "failed",
                float(row.get("score") or 0.0),
                row_created_at,
                ts,
                json.dumps({**row, "check_key": check_key, "run_id": run_id}, ensure_ascii=False, default=str),
            ),
        )
    conn.commit()
    return run_id


def list_experience_checks(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT check_key, created_at, updated_at, check_json
        FROM experience_checks
        WHERE run_id = ?
        ORDER BY updated_at DESC, scenario_id ASC
        """,
        (str(run_id or "").strip(),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["check_json"] or "{}")
        except Exception:
            payload = {}
        payload["check_key"] = str(row["check_key"] or "")
        payload["created_at"] = float(row["created_at"] or 0.0)
        payload["updated_at"] = float(row["updated_at"] or 0.0)
        out.append(payload)
    return out


def get_experience_run(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT run_id, actor, status, passed, summary, total_checks, failed_checks,
               average_score, created_at, updated_at, run_json
        FROM experience_runs
        WHERE run_id = ?
        LIMIT 1
        """,
        (str(run_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["run_json"] or "{}")
    except Exception:
        payload = {}
    return {
        **payload,
        "run_id": str(row["run_id"] or ""),
        "actor": str(row["actor"] or ""),
        "status": str(row["status"] or ""),
        "passed": bool(row["passed"]),
        "summary": str(row["summary"] or ""),
        "total_checks": int(row["total_checks"] or 0),
        "failed_checks": int(row["failed_checks"] or 0),
        "average_score": float(row["average_score"] or 0.0),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "checks": list_experience_checks(conn, str(row["run_id"] or "")),
    }


def list_experience_runs(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT run_id
        FROM experience_runs
        WHERE (? = '' OR status = ?)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(status or "").strip(), str(status or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [get_experience_run(conn, str(row["run_id"])) for row in rows]


def get_latest_experience_run(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = list_experience_runs(conn, limit=1)
    return rows[0] if rows else {}


def save_repair_attempt(conn: sqlite3.Connection, attempt: Dict[str, Any]) -> str:
    payload = dict(attempt)
    attempt_id = str(payload.get("attempt_id") or "").strip() or new_repair_attempt_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO repair_attempts(
            attempt_id, incident_id, attempt_no, stage, model_used, status, branch,
            worktree_path, created_at, updated_at, attempt_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(attempt_id) DO UPDATE SET
            incident_id = excluded.incident_id,
            attempt_no = excluded.attempt_no,
            stage = excluded.stage,
            model_used = excluded.model_used,
            status = excluded.status,
            branch = excluded.branch,
            worktree_path = excluded.worktree_path,
            updated_at = excluded.updated_at,
            attempt_json = excluded.attempt_json
        """,
        (
            attempt_id,
            str(payload.get("incident_id") or "").strip(),
            int(payload.get("attempt_number") or payload.get("attempt_no") or 0),
            str(payload.get("stage") or "").strip(),
            str(payload.get("model_used") or "").strip(),
            str(payload.get("status") or "pending").strip(),
            str(payload.get("branch") or "").strip(),
            str(payload.get("worktree_path") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "attempt_id": attempt_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return attempt_id


def list_repair_attempts(conn: sqlite3.Connection, incident_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT attempt_id, created_at, updated_at, attempt_json
        FROM repair_attempts
        WHERE incident_id = ?
        ORDER BY attempt_no ASC, updated_at ASC
        """,
        (str(incident_id or "").strip(),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["attempt_json"] or "{}")
        except Exception:
            payload = {}
        payload["attempt_id"] = str(row["attempt_id"] or "")
        payload["created_at"] = float(row["created_at"] or 0.0)
        payload["updated_at"] = float(row["updated_at"] or 0.0)
        out.append(payload)
    return out


def save_repair_plan(conn: sqlite3.Connection, plan: Dict[str, Any]) -> str:
    payload = dict(plan)
    plan_id = str(payload.get("plan_id") or "").strip() or new_repair_plan_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO repair_plans(
            plan_id, incident_id, status, model_used, created_at, updated_at, plan_json
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(plan_id) DO UPDATE SET
            incident_id = excluded.incident_id,
            status = excluded.status,
            model_used = excluded.model_used,
            updated_at = excluded.updated_at,
            plan_json = excluded.plan_json
        """,
        (
            plan_id,
            str(payload.get("incident_id") or "").strip(),
            str(payload.get("status") or "planned").strip(),
            str(payload.get("model_used") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "plan_id": plan_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return plan_id


def get_latest_repair_plan(conn: sqlite3.Connection, incident_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT plan_id, created_at, updated_at, plan_json
        FROM repair_plans
        WHERE incident_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(incident_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["plan_json"] or "{}")
    except Exception:
        payload = {}
    payload["plan_id"] = str(row["plan_id"] or "")
    payload["created_at"] = float(row["created_at"] or 0.0)
    payload["updated_at"] = float(row["updated_at"] or 0.0)
    return payload


def count_principals(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM principals").fetchone()
    return int(row["n"] or 0) if row else 0


def count_active_memories(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM principal_memories WHERE is_active = 1"
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def count_pending_reminders(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reminders WHERE status IN ('scheduled', 'awaiting_delivery_channel')"
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def count_due_reminders(conn: sqlite3.Connection, *, now_ts: Optional[float] = None) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM reminders
        WHERE status IN ('scheduled', 'awaiting_delivery_channel')
          AND due_at <= ?
        """,
        (float(now_ts or time.time()),),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def load_recent_telegram_history(
    conn: sqlite3.Connection,
    chat_id: Any,
    *,
    limit_turns: int = 6,
    exclude_task_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    if chat_id is None:
        return []
    cid = str(chat_id).strip()
    params: List[Any] = [Channel.TELEGRAM.value, EventType.USER_MESSAGE.value, cid]
    exclude_clause = ""
    if exclude_task_id:
        exclude_clause = "AND e.task_id != ?"
        params.append(exclude_task_id)
    params.append(max(1, int(limit_turns)))
    rows = conn.execute(
        f"""
        SELECT e.task_id, MAX(e.seq) AS last_seq
        FROM events e
        JOIN tasks t ON t.task_id = e.task_id
        WHERE t.channel = ?
          AND e.event_type = ?
          AND CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) = ?
          {exclude_clause}
        GROUP BY e.task_id
        ORDER BY last_seq DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    task_ids = [str(row["task_id"]) for row in reversed(rows)]
    history: List[Dict[str, str]] = []
    for task_id in task_ids:
        user_text = ""
        assistant_text = ""
        assistant_source = ""
        for _seq, _ts, et_raw, payload in load_events_for_task(conn, task_id):
            if et_raw == EventType.USER_MESSAGE.value and payload.get("text"):
                user_text = _clip_text(payload.get("routing_text") or payload.get("text"))
            elif et_raw == EventType.ASSISTANT_REPLIED.value and payload.get("text"):
                assistant_text = _clip_text(payload.get("text"))
                assistant_source = "direct"
            elif et_raw == EventType.JOB_COMPLETED.value and payload.get("summary"):
                backend = str(payload.get("backend") or "").strip()
                delegated = bool(payload.get("delegated_to_cursor"))
                if backend == "openclaw" and delegated:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw_cursor"
                elif backend == "openclaw":
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw"
                else:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "cursor"
            elif et_raw == EventType.JOB_FAILED.value:
                detail = payload.get("message") or payload.get("error")
                if detail:
                    backend = str(payload.get("backend") or "").strip()
                    if backend == "openclaw":
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "openclaw"
                    else:
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "cursor"
        if user_text:
            history.append({"role": "user", "content": user_text, "task_id": task_id})
        if assistant_text:
            history.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "task_id": task_id,
                    "source": assistant_source or "direct",
                }
            )
    return history


def task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row is not None


def get_task_channel(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT channel FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return str(row["channel"]) if row else None


def list_tasks(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT task_id, channel, created_at, updated_at FROM tasks ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_task_updated_at(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    row = conn.execute(
        "SELECT updated_at FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return float(row["updated_at"]) if row else None


def list_recent_telegram_task_ids(conn: sqlite3.Connection, limit: int = 25) -> List[str]:
    """Most recently touched Telegram tasks (excluding the reserved system task)."""
    rows = conn.execute(
        """
        SELECT task_id FROM tasks
        WHERE channel = ? AND task_id != ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (Channel.TELEGRAM.value, SYSTEM_TASK_ID, limit),
    ).fetchall()
    return [str(r["task_id"]) for r in rows]


def list_telegram_task_ids_for_chat(
    conn: sqlite3.Connection,
    chat_id: Any,
    *,
    limit: int = 25,
) -> List[str]:
    """
    Telegram tasks that have at least one UserMessage with this chat_id,
    ordered by task updated_at (best-effort per-chat continuation lookup).
    """
    if chat_id is None:
        return []
    cid = str(chat_id).strip()
    rows = conn.execute(
        """
        SELECT t.task_id
        FROM tasks t
        WHERE t.channel = ?
          AND t.task_id != ?
          AND EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.task_id
              AND e.event_type = ?
              AND CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) = ?
          )
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        (
            Channel.TELEGRAM.value,
            SYSTEM_TASK_ID,
            EventType.USER_MESSAGE.value,
            cid,
            limit,
        ),
    ).fetchall()
    return [str(r["task_id"]) for r in rows]


def new_goal_id() -> str:
    return f"gol_{uuid.uuid4().hex[:26]}"


def create_goal(
    conn: sqlite3.Connection,
    principal_id: str,
    summary: str,
    *,
    channel: str = "internal",
    metadata: Optional[Dict[str, Any]] = None,
    status: str = "active",
) -> str:
    gid = new_goal_id()
    ts = time.time()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO goals(
            goal_id, principal_id, channel, status, summary, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (gid, principal_id, channel, status, summary[:2000], meta_json, ts, ts),
    )
    conn.commit()
    return gid


def update_goal_status(conn: sqlite3.Connection, goal_id: str, status: str) -> bool:
    ts = time.time()
    cur = conn.execute(
        "UPDATE goals SET status = ?, updated_at = ? WHERE goal_id = ?",
        (status, ts, goal_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_goal(conn: sqlite3.Connection, goal_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def list_goals_for_principal(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    if status:
        rows = conn.execute(
            """
            SELECT * FROM goals
            WHERE principal_id = ? AND status = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (principal_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM goals
            WHERE principal_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (principal_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def append_goal_event(
    conn: sqlite3.Connection, goal_id: str, event_type: str, payload: Dict[str, Any]
) -> int:
    ts = time.time()
    cur = conn.execute(
        "INSERT INTO goal_events(goal_id, ts, event_type, payload_json) VALUES (?,?,?,?)",
        (goal_id, ts, event_type, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def link_task_to_goal(conn: sqlite3.Connection, task_id: str, goal_id: str) -> None:
    ts = time.time()
    conn.execute(
        """
        INSERT INTO task_goals(task_id, goal_id, created_at) VALUES (?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET goal_id = excluded.goal_id, created_at = excluded.created_at
        """,
        (task_id, goal_id, ts),
    )
    conn.commit()


def get_goal_id_for_task(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT goal_id FROM task_goals WHERE task_id = ?", (task_id,)
    ).fetchone()
    return str(row["goal_id"]) if row else None


def list_tasks_for_goal(
    conn: sqlite3.Connection, goal_id: str, *, limit: int = 20
) -> List[str]:
    rows = conn.execute(
        """
        SELECT tg.task_id FROM task_goals tg
        JOIN tasks t ON t.task_id = tg.task_id
        WHERE tg.goal_id = ?
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        (goal_id, limit),
    ).fetchall()
    return [str(r["task_id"]) for r in rows]


def record_goal_artifact(
    conn: sqlite3.Connection,
    goal_id: str,
    *,
    task_id: str = "",
    kind: str = "file",
    label: str = "",
    uri: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    aid = f"art_{uuid.uuid4().hex[:24]}"
    ts = time.time()
    conn.execute(
        """
        INSERT INTO goal_artifacts(
            artifact_id, goal_id, task_id, kind, label, uri, metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            aid,
            goal_id,
            task_id,
            kind,
            label[:500],
            uri[:2000],
            json.dumps(metadata or {}, ensure_ascii=False),
            ts,
        ),
    )
    conn.commit()
    return aid


def list_goal_artifacts(
    conn: sqlite3.Connection, goal_id: str, *, limit: int = 50
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM goal_artifacts
        WHERE goal_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (goal_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def new_execution_attempt_id() -> str:
    return f"atm_{uuid.uuid4().hex[:20]}"


def supersede_active_execution_attempts(conn: sqlite3.Connection, task_id: str) -> None:
    ts = time.time()
    conn.execute(
        """
        UPDATE execution_attempts
        SET status = 'superseded', updated_at = ?
        WHERE task_id = ? AND status = 'active'
        """,
        (ts, task_id),
    )


def create_execution_attempt(
    conn: sqlite3.Connection,
    task_id: str,
    goal_id: str,
    *,
    lane: str,
    backend: str,
    handle_dict: Dict[str, Any],
    parent_attempt_id: str = "",
) -> str:
    """Create a new active execution attempt after superseding prior actives for the task."""
    supersede_active_execution_attempts(conn, task_id)
    eid = new_execution_attempt_id()
    ts = time.time()
    conn.execute(
        """
        INSERT INTO execution_attempts(
            exec_attempt_id, task_id, goal_id, lane, backend, status,
            handle_json, summary_json, parent_attempt_id,
            continuation_state, verification_state, recovery_state,
            created_at, updated_at, last_synced_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            eid,
            task_id,
            goal_id or "",
            lane or "",
            backend or "",
            "active",
            json.dumps(handle_dict or {}, ensure_ascii=False),
            "{}",
            parent_attempt_id or "",
            "",
            "",
            "",
            ts,
            ts,
            0.0,
            0.0,
        ),
    )
    conn.commit()
    return eid


def get_active_execution_attempt_for_task(
    conn: sqlite3.Connection, task_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM execution_attempts
        WHERE task_id = ? AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def get_execution_attempt(conn: sqlite3.Connection, exec_attempt_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM execution_attempts WHERE exec_attempt_id = ?",
        (exec_attempt_id,),
    ).fetchone()
    return dict(row) if row else None


def update_execution_attempt_handles(
    conn: sqlite3.Connection,
    exec_attempt_id: str,
    handle_patch: Dict[str, Any],
    *,
    touch_last_synced: bool = False,
) -> bool:
    row = conn.execute(
        "SELECT handle_json FROM execution_attempts WHERE exec_attempt_id = ?",
        (exec_attempt_id,),
    ).fetchone()
    if not row:
        return False
    try:
        cur = json.loads(row["handle_json"] or "{}")
    except json.JSONDecodeError:
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(handle_patch)
    ts = time.time()
    if touch_last_synced:
        conn.execute(
            """
            UPDATE execution_attempts
            SET handle_json = ?, updated_at = ?, last_synced_at = ?
            WHERE exec_attempt_id = ?
            """,
            (json.dumps(cur, ensure_ascii=False), ts, ts, exec_attempt_id),
        )
    else:
        conn.execute(
            """
            UPDATE execution_attempts
            SET handle_json = ?, updated_at = ?
            WHERE exec_attempt_id = ?
            """,
            (json.dumps(cur, ensure_ascii=False), ts, exec_attempt_id),
        )
    conn.commit()
    return True


def complete_execution_attempt(
    conn: sqlite3.Connection,
    exec_attempt_id: str,
    status: str,
    summary: Optional[Dict[str, Any]] = None,
) -> bool:
    row = get_execution_attempt(conn, exec_attempt_id)
    if not row:
        return False
    ts = time.time()
    merged: Dict[str, Any] = {}
    try:
        merged = json.loads(row["summary_json"] or "{}")
    except json.JSONDecodeError:
        merged = {}
    if not isinstance(merged, dict):
        merged = {}
    if summary:
        merged.update(summary)
    conn.execute(
        """
        UPDATE execution_attempts
        SET status = ?, completed_at = ?, updated_at = ?, summary_json = ?
        WHERE exec_attempt_id = ?
        """,
        (status, ts, ts, json.dumps(merged, ensure_ascii=False), exec_attempt_id),
    )
    conn.commit()
    return True


def list_execution_attempts_for_goal(
    conn: sqlite3.Connection, goal_id: str, *, limit: int = 20
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM execution_attempts
        WHERE goal_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (goal_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count_active_execution_attempts(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM execution_attempts WHERE status = 'active'"
    ).fetchone()
    return int(row["c"]) if row else 0


def new_workflow_id() -> str:
    return f"wfl_{uuid.uuid4().hex[:24]}"


def create_workflow(
    conn: sqlite3.Connection,
    principal_id: str,
    name: str,
    *,
    definition: Optional[Dict[str, Any]] = None,
    status: str = "draft",
    next_run_at: float = 0.0,
) -> str:
    wid = new_workflow_id()
    ts = time.time()
    conn.execute(
        """
        INSERT INTO workflows(
            workflow_id, principal_id, status, name, definition_json, next_run_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            wid,
            principal_id,
            status,
            name[:500],
            json.dumps(definition or {}, ensure_ascii=False),
            float(next_run_at),
            ts,
            ts,
        ),
    )
    conn.commit()
    return wid


def get_workflow(conn: sqlite3.Connection, workflow_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    return dict(row) if row else None


def update_workflow(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    status: Optional[str] = None,
    definition: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[float] = None,
) -> bool:
    row = get_workflow(conn, workflow_id)
    if not row:
        return False
    ts = time.time()
    st = status if status is not None else str(row["status"])
    def_json = (
        json.dumps(definition, ensure_ascii=False)
        if definition is not None
        else str(row["definition_json"])
    )
    nrun = float(next_run_at) if next_run_at is not None else float(row["next_run_at"] or 0)
    conn.execute(
        """
        UPDATE workflows
        SET status = ?, definition_json = ?, next_run_at = ?, updated_at = ?
        WHERE workflow_id = ?
        """,
        (st, def_json, nrun, ts, workflow_id),
    )
    conn.commit()
    return True


def list_workflows_for_principal(
    conn: sqlite3.Connection, principal_id: str, *, limit: int = 20
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM workflows
        WHERE principal_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (principal_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Execution plans / plan steps / verification (orchestrator) ---


def new_plan_id() -> str:
    return f"pln_{uuid.uuid4().hex[:20]}"


def new_plan_step_id() -> str:
    return f"pst_{uuid.uuid4().hex[:20]}"


def new_verification_id() -> str:
    return f"ver_{uuid.uuid4().hex[:20]}"


def new_goal_approval_id() -> str:
    return f"gap_{uuid.uuid4().hex[:20]}"


def insert_execution_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    task_id: str,
    *,
    goal_id: str = "",
    principal_id: str = "",
    intent_summary: str = "",
    plan_kind: str = "delegated_repo_task",
    status: str = "draft",
    risk_tier: int = 2,
    approval_state: str = "none",
    verification_state: str = "pending",
    recovery_state: str = "",
    current_step_id: str = "",
    router_snapshot: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> None:
    ts = time.time()
    conn.execute(
        """
        INSERT INTO execution_plans(
            plan_id, goal_id, task_id, principal_id, intent_summary, plan_kind,
            status, risk_tier, approval_state, verification_state, recovery_state,
            current_step_id, router_snapshot_json, summary_json, created_at, updated_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            plan_id,
            goal_id or "",
            task_id,
            principal_id or "",
            intent_summary[:2000] if intent_summary else "",
            plan_kind or "delegated_repo_task",
            status,
            int(risk_tier),
            approval_state,
            verification_state,
            recovery_state or "",
            current_step_id or "",
            json.dumps(router_snapshot or {}, ensure_ascii=False),
            json.dumps(summary or {}, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    conn.commit()


def insert_plan_step(
    conn: sqlite3.Connection,
    step_id: str,
    plan_id: str,
    ordinal: int,
    *,
    title: str = "",
    step_kind: str = "",
    lane: str = "",
    action: Optional[Dict[str, Any]] = None,
    policy: Optional[Dict[str, Any]] = None,
    status: str = "pending",
    execution_attempt_id: str = "",
    checkpoint: Optional[Dict[str, Any]] = None,
    recovery: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    ts = time.time()
    conn.execute(
        """
        INSERT INTO plan_steps(
            step_id, plan_id, ordinal, title, step_kind, lane,
            action_json, policy_json, status, execution_attempt_id,
            checkpoint_json, recovery_json, result_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            step_id,
            plan_id,
            int(ordinal),
            title[:500] if title else "",
            step_kind or "",
            lane or "",
            json.dumps(action or {}, ensure_ascii=False),
            json.dumps(policy or {}, ensure_ascii=False),
            status,
            execution_attempt_id or "",
            json.dumps(checkpoint or {}, ensure_ascii=False),
            json.dumps(recovery or {}, ensure_ascii=False),
            json.dumps(result or {}, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    conn.commit()


def get_execution_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["router_snapshot"] = json.loads(d.pop("router_snapshot_json") or "{}")
    d["summary"] = json.loads(d.pop("summary_json") or "{}")
    return d


def get_active_execution_plan_for_task(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM execution_plans
        WHERE task_id = ?
          AND status NOT IN ('completed', 'failed', 'abandoned')
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["router_snapshot"] = json.loads(d.pop("router_snapshot_json") or "{}")
    d["summary"] = json.loads(d.pop("summary_json") or "{}")
    return d


def list_recent_execution_plans(conn: sqlite3.Connection, *, limit: int = 30) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM execution_plans
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["router_snapshot"] = json.loads(d.pop("router_snapshot_json") or "{}")
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        out.append(d)
    return out


def update_execution_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    status: Optional[str] = None,
    approval_state: Optional[str] = None,
    verification_state: Optional[str] = None,
    recovery_state: Optional[str] = None,
    current_step_id: Optional[str] = None,
    summary_patch: Optional[Dict[str, Any]] = None,
    completed: Optional[bool] = None,
) -> bool:
    row = conn.execute(
        "SELECT * FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if not row:
        return False
    d = dict(row)
    ts = time.time()
    st = status if status is not None else str(d["status"])
    ap = approval_state if approval_state is not None else str(d["approval_state"])
    vs = verification_state if verification_state is not None else str(d["verification_state"])
    rs = recovery_state if recovery_state is not None else str(d["recovery_state"])
    cs = current_step_id if current_step_id is not None else str(d["current_step_id"])
    summ = json.loads(d["summary_json"] or "{}")
    if not isinstance(summ, dict):
        summ = {}
    if summary_patch:
        summ.update(summary_patch)
    comp_at = float(d["completed_at"] or 0)
    if completed is True:
        comp_at = ts
    conn.execute(
        """
        UPDATE execution_plans SET
            status = ?, approval_state = ?, verification_state = ?, recovery_state = ?,
            current_step_id = ?, summary_json = ?, updated_at = ?, completed_at = ?
        WHERE plan_id = ?
        """,
        (
            st,
            ap,
            vs,
            rs,
            cs,
            json.dumps(summ, ensure_ascii=False),
            ts,
            comp_at,
            plan_id,
        ),
    )
    conn.commit()
    return True


def list_plan_steps(conn: sqlite3.Connection, plan_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY ordinal ASC
        """,
        (plan_id,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["action"] = json.loads(d.pop("action_json") or "{}")
        d["policy"] = json.loads(d.pop("policy_json") or "{}")
        d["checkpoint"] = json.loads(d.pop("checkpoint_json") or "{}")
        d["recovery"] = json.loads(d.pop("recovery_json") or "{}")
        d["result"] = json.loads(d.pop("result_json") or "{}")
        out.append(d)
    return out


def get_plan_step(conn: sqlite3.Connection, step_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM plan_steps WHERE step_id = ?", (step_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["action"] = json.loads(d.pop("action_json") or "{}")
    d["policy"] = json.loads(d.pop("policy_json") or "{}")
    d["checkpoint"] = json.loads(d.pop("checkpoint_json") or "{}")
    d["recovery"] = json.loads(d.pop("recovery_json") or "{}")
    d["result"] = json.loads(d.pop("result_json") or "{}")
    return d


def update_plan_step(
    conn: sqlite3.Connection,
    step_id: str,
    *,
    status: Optional[str] = None,
    execution_attempt_id: Optional[str] = None,
    action_patch: Optional[Dict[str, Any]] = None,
    recovery_patch: Optional[Dict[str, Any]] = None,
    result_patch: Optional[Dict[str, Any]] = None,
    checkpoint_patch: Optional[Dict[str, Any]] = None,
) -> bool:
    row = conn.execute("SELECT * FROM plan_steps WHERE step_id = ?", (step_id,)).fetchone()
    if not row:
        return False
    d = dict(row)
    ts = time.time()
    st = status if status is not None else str(d["status"])
    eid = (
        execution_attempt_id
        if execution_attempt_id is not None
        else str(d["execution_attempt_id"] or "")
    )
    action = json.loads(d["action_json"] or "{}")
    if not isinstance(action, dict):
        action = {}
    if action_patch:
        action.update(action_patch)
    recovery = json.loads(d["recovery_json"] or "{}")
    if not isinstance(recovery, dict):
        recovery = {}
    if recovery_patch:
        recovery.update(recovery_patch)
    result = json.loads(d["result_json"] or "{}")
    if not isinstance(result, dict):
        result = {}
    if result_patch:
        result.update(result_patch)
    checkpoint = json.loads(d["checkpoint_json"] or "{}")
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    if checkpoint_patch:
        checkpoint.update(checkpoint_patch)
    conn.execute(
        """
        UPDATE plan_steps SET
            status = ?, execution_attempt_id = ?,
            action_json = ?, recovery_json = ?, result_json = ?, checkpoint_json = ?, updated_at = ?
        WHERE step_id = ?
        """,
        (
            st,
            eid,
            json.dumps(action, ensure_ascii=False),
            json.dumps(recovery, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            json.dumps(checkpoint, ensure_ascii=False),
            ts,
            step_id,
        ),
    )
    conn.commit()
    return True


def insert_verification_result(
    conn: sqlite3.Connection,
    verification_id: str,
    *,
    plan_id: str,
    step_id: str,
    method: str,
    verdict: str,
    summary: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    ts = time.time()
    conn.execute(
        """
        INSERT INTO verification_results(
            verification_id, plan_id, step_id, method, verdict, summary, evidence_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            verification_id,
            plan_id or "",
            step_id or "",
            method or "",
            verdict or "",
            summary[:2000] if summary else "",
            json.dumps(evidence or {}, ensure_ascii=False),
            ts,
        ),
    )
    conn.commit()


def list_verification_results_for_plan(
    conn: sqlite3.Connection, plan_id: str, *, limit: int = 20
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM verification_results
        WHERE plan_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (plan_id, limit),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        out.append(d)
    return out


def create_goal_approval(
    conn: sqlite3.Connection,
    goal_id: str,
    task_id: str,
    *,
    rationale: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    aid = new_goal_approval_id()
    ts = time.time()
    meta = dict(metadata or {})
    conn.execute(
        """
        INSERT INTO goal_approvals(
            approval_id, goal_id, task_id, status, rationale, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            aid,
            goal_id or "",
            task_id or "",
            "pending",
            rationale[:2000] if rationale else "",
            json.dumps(meta, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    conn.commit()
    return aid


def get_goal_approval(conn: sqlite3.Connection, approval_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM goal_approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
    return d


def update_goal_approval_status(
    conn: sqlite3.Connection,
    approval_id: str,
    status: str,
    *,
    rationale_patch: Optional[str] = None,
) -> bool:
    row = conn.execute(
        "SELECT * FROM goal_approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    if not row:
        return False
    ts = time.time()
    rat = str(row["rationale"] or "")
    if rationale_patch is not None:
        rat = rationale_patch[:2000]
    conn.execute(
        """
        UPDATE goal_approvals SET status = ?, rationale = ?, updated_at = ?
        WHERE approval_id = ?
        """,
        (status, rat, ts, approval_id),
    )
    conn.commit()
    return True


def list_pending_goal_approvals_for_task(
    conn: sqlite3.Connection, task_id: str
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM goal_approvals
        WHERE task_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        """,
        (task_id,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        out.append(d)
    return out


SYSTEM_TASK_ID = "tsk_system_lockstep"


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def ensure_system_task(conn: sqlite3.Connection) -> None:
    """Reserved task row for global audit events (capabilities, kill switch)."""
    if task_exists(conn, SYSTEM_TASK_ID):
        return
    create_task(conn, SYSTEM_TASK_ID, "internal")
