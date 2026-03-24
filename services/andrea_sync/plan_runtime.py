"""Plan creation, approval gating, verification hooks, and bounded recovery metadata."""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .activation_policy import (
    ACTIVATION_POLICY_VERSION,
    MEASURED_SCENARIOS,
    collab_policy_recording_enabled,
    evaluate_activation_policy,
)
from .approval_policy import evaluate_plan_step_approval, risk_tier_for_lane
from .arbitration_policy import (
    build_collaboration_bundle,
    collaboration_layer_enabled,
    collaboration_summary_patch,
    explain_collaboration_attachment_blockers,
)
from . import collaboration_runtime
from .collaboration_effectiveness import (
    build_collaboration_outcome_payload,
    build_repair_outcome_payload,
    canonical_usefulness_class,
)
from .collaboration_promotion import evaluate_promotion_guardrails_after_outcome
from .collaboration_schema import collaboration_event_payload
from .plan_schema import PlanStatus, StepKind, StepStatus
from .recovery_engine import suggest_recovery
from .tool_registry import manifest_for_lane
from .persona_policy import (
    collaboration_repair_user_note,
    scenario_approval_intro,
    scenario_verification_footer,
)
from .scenario_registry import default_contract, get_contract
from .scenario_runtime import (
    proof_signals_satisfied_for_trusted_completion,
    stored_plan_kind_for_delegate_contract,
    trusted_receipt_allowed,
)
from .scenario_schema import merge_scenario_into_plan_summary
from .user_surface import format_scenario_proof_receipt
from .verification_policy import (
    evaluate_delegated_repo_outcome,
    verification_method_for_scenario,
)
from .store import (
    create_goal,
    create_goal_approval,
    get_active_execution_plan_for_task,
    get_execution_plan,
    get_goal_approval,
    get_goal_id_for_task,
    get_plan_step,
    get_task_channel,
    insert_execution_plan,
    insert_plan_step,
    insert_collaboration_activation_decision,
    insert_collaboration_outcome_row,
    insert_repair_outcome_row,
    insert_verification_result,
    link_task_to_goal,
    list_plan_steps,
    new_plan_id,
    new_plan_step_id,
    new_verification_id,
    update_execution_plan,
    update_goal_approval_status,
    update_plan_step,
)


def plan_orchestrator_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_PLAN_ORCHESTRATOR") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def force_delegate_approval() -> bool:
    return (os.environ.get("ANDREA_SYNC_FORCE_DELEGATE_APPROVAL") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def strict_post_execution_verification() -> bool:
    """When true, `needs_human` verification blocks JOB_COMPLETED. Default off for backward compatibility."""
    v = (os.environ.get("ANDREA_SYNC_STRICT_VERIFICATION") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class DelegateGateResult:
    mode: str  # proceed | await_approval
    plan_id: str = ""
    execute_step_id: str = ""
    approval_id: str = ""
    user_message: str = ""
    rationale: str = ""


def _template_steps(
    plan_id: str, execution_lane: str, job_payload: Dict[str, Any]
) -> List[Tuple[str, int, str, str, str, Dict[str, Any]]]:
    """Returns tuples: step_id, ordinal, title, step_kind, lane, action."""
    exec_step = new_plan_step_id()
    verify_step = new_plan_step_id()
    summarize_step = new_plan_step_id()
    actions_exec = {
        "pending_job_payload": dict(job_payload),
        "runner": job_payload.get("runner"),
        "kind": job_payload.get("kind"),
    }
    return [
        (
            exec_step,
            1,
            "Delegated execution",
            StepKind.EXECUTE_DELEGATED.value,
            execution_lane,
            actions_exec,
        ),
        (
            verify_step,
            2,
            "Verify repo outcome",
            StepKind.VERIFY_REPO.value,
            execution_lane,
            {},
        ),
        (
            summarize_step,
            3,
            "Summarize",
            StepKind.SUMMARIZE.value,
            execution_lane,
            {},
        ),
    ]


def ensure_delegate_plan(
    conn: sqlite3.Connection,
    task_id: str,
    goal_id: str,
    principal_id: str,
    intent_summary: str,
    execution_lane: str,
    job_payload: Dict[str, Any],
    router_ranks: List[Any],
) -> Tuple[str, str]:
    """
    Create or reuse the active plan for this task. Returns (plan_id, execute_step_id).
    """
    existing = get_active_execution_plan_for_task(conn, task_id)
    if existing:
        pid = str(existing["plan_id"])
        scen = job_payload.get("scenario")
        if isinstance(scen, dict):
            ex = get_execution_plan(conn, pid)
            prev = ex.get("summary") if isinstance(ex, dict) else {}
            if not isinstance(prev, dict):
                prev = {}
            update_execution_plan(
                conn,
                pid,
                summary_patch=merge_scenario_into_plan_summary(prev, scen),
            )
        steps = list_plan_steps(conn, pid)
        for s in steps:
            if str(s.get("step_kind") or "") == StepKind.EXECUTE_DELEGATED.value:
                # Refresh pending payload for the execute step while still draft/awaiting
                if str(s.get("status") or "") in (
                    StepStatus.PENDING.value,
                    StepStatus.AWAITING_APPROVAL.value,
                    StepStatus.APPROVED.value,
                    StepStatus.QUEUED.value,
                ):
                    update_plan_step(
                        conn,
                        str(s["step_id"]),
                        action_patch={"pending_job_payload": dict(job_payload)},
                    )
                return pid, str(s["step_id"])
        # Fallback: first step
        if steps:
            return pid, str(steps[0]["step_id"])
        return pid, ""

    pid = new_plan_id()
    tier = risk_tier_for_lane(execution_lane)
    router_snapshot = {
        "ranks": router_ranks[:8] if isinstance(router_ranks, list) else [],
        "chosen_lane": execution_lane,
    }
    base_summary: Dict[str, Any] = {"source": "delegate_route"}
    scen = job_payload.get("scenario")
    scen_contract = None
    if isinstance(scen, dict):
        base_summary = merge_scenario_into_plan_summary(base_summary, scen)
        scen_contract = get_contract(str(scen.get("scenario_id") or ""))
    if scen_contract is None:
        scen_contract = default_contract()
    plan_kind_value = stored_plan_kind_for_delegate_contract(scen_contract)
    insert_execution_plan(
        conn,
        pid,
        task_id,
        goal_id=goal_id or "",
        principal_id=principal_id or "",
        intent_summary=intent_summary[:2000] if intent_summary else "",
        plan_kind=plan_kind_value,
        status=PlanStatus.DRAFT.value,
        risk_tier=tier,
        approval_state="none",
        verification_state="pending",
        router_snapshot=router_snapshot,
        summary=base_summary,
    )
    exec_id = ""
    for sid, ord_, title, sk, lane, action in _template_steps(
        pid, execution_lane, job_payload
    ):
        insert_plan_step(
            conn,
            sid,
            pid,
            ord_,
            title=title,
            step_kind=sk,
            lane=lane,
            action=action,
            policy=dict(manifest_for_lane(lane)),
            status=StepStatus.PENDING.value,
        )
        if sk == StepKind.EXECUTE_DELEGATED.value:
            exec_id = sid
    update_execution_plan(conn, pid, current_step_id=exec_id)
    return pid, exec_id


def gate_delegated_job(
    conn: sqlite3.Connection,
    task_id: str,
    goal_id: str,
    principal_id: str,
    intent_summary: str,
    execution_lane: str,
    job_payload: Dict[str, Any],
    router_ranks: List[Any],
) -> DelegateGateResult:
    """
    Enforce approval policy before JOB_QUEUED. Mutates store when approval is required.
    """
    if not plan_orchestrator_enabled():
        return DelegateGateResult(mode="proceed")

    plan_id, execute_step_id = ensure_delegate_plan(
        conn,
        task_id,
        goal_id,
        principal_id,
        intent_summary,
        execution_lane,
        job_payload,
        router_ranks,
    )
    if not execute_step_id:
        return DelegateGateResult(mode="proceed")

    step_row = get_plan_step(conn, execute_step_id)
    if not step_row:
        return DelegateGateResult(mode="proceed")

    if str(step_row.get("status") or "") in (
        StepStatus.APPROVED.value,
        StepStatus.QUEUED.value,
    ):
        update_execution_plan(
            conn,
            plan_id,
            status=PlanStatus.QUEUED.value,
            approval_state="granted",
            current_step_id=execute_step_id,
        )
        update_plan_step(conn, execute_step_id, status=StepStatus.QUEUED.value)
        return DelegateGateResult(
            mode="proceed", plan_id=plan_id, execute_step_id=execute_step_id
        )

    scen_payload = job_payload.get("scenario")
    scenario_kw = scen_payload if isinstance(scen_payload, dict) else None
    pol = evaluate_plan_step_approval(
        lane=execution_lane,
        step_kind=StepKind.EXECUTE_DELEGATED.value,
        command_type="delegate",
        force_approval=force_delegate_approval(),
        scenario=scenario_kw,
    )
    if not pol.get("needs_approval"):
        update_execution_plan(
            conn,
            plan_id,
            status=PlanStatus.QUEUED.value,
            approval_state="auto",
            current_step_id=execute_step_id,
        )
        update_plan_step(conn, execute_step_id, status=StepStatus.QUEUED.value)
        return DelegateGateResult(
            mode="proceed", plan_id=plan_id, execute_step_id=execute_step_id
        )

    # Pending human approval (goal_approvals FK requires a real goals row)
    effective_goal = goal_id or get_goal_id_for_task(conn, task_id) or ""
    if not effective_goal and principal_id:
        ch = get_task_channel(conn, task_id) or "cli"
        effective_goal = create_goal(
            conn,
            principal_id,
            (intent_summary or "Delegated execution")[:240],
            channel=ch,
            metadata={"source": "plan_orchestrator"},
        )
        link_task_to_goal(conn, task_id, effective_goal)

    # Pending human approval
    scope = f"lane={execution_lane}; plan={plan_id}; step={execute_step_id}"
    meta_approval = {
        "plan_id": plan_id,
        "step_id": execute_step_id,
        "scope_summary": scope,
        "requested_lane": execution_lane,
        "requested_action_class": "execute_delegated",
        "risk_tier": pol.get("risk_tier"),
    }
    if scenario_kw:
        meta_approval["scenario_id"] = str(scenario_kw.get("scenario_id") or "")
        meta_approval["proof_class"] = str(scenario_kw.get("proof_class") or "")
    approval_id = create_goal_approval(
        conn,
        effective_goal or "",
        task_id,
        rationale=str(pol.get("rationale") or "policy"),
        metadata=meta_approval,
    )
    update_execution_plan(
        conn,
        plan_id,
        status=PlanStatus.AWAITING_APPROVAL.value,
        approval_state="pending",
        current_step_id=execute_step_id,
    )
    update_plan_step(conn, execute_step_id, status=StepStatus.AWAITING_APPROVAL.value)
    scen_label = ""
    intro = ""
    if scenario_kw:
        scen_label = str(scenario_kw.get("scenario_id") or "").strip()
        cappr = get_contract(scen_label)
        if cappr and cappr.approval_mode == "required" and cappr.user_facing_label:
            intro = scenario_approval_intro(cappr.user_facing_label) + "\n\n"
    scen_bit = f" Scenario: `{scen_label}`." if scen_label else ""
    msg = (
        intro
        + f"I have a governed execution plan (`{plan_id}`). "
        f"The delegated execution step needs your approval ({pol.get('rationale') or 'policy'}).{scen_bit} "
        f"Reply with approval or use ResolveGoalApproval for `{approval_id}`."
    )
    return DelegateGateResult(
        mode="await_approval",
        plan_id=plan_id,
        execute_step_id=execute_step_id,
        approval_id=approval_id,
        user_message=msg,
        rationale=str(pol.get("rationale") or ""),
    )


def resolve_goal_approval_command(
    conn: sqlite3.Connection,
    task_id: str,
    approval_id: str,
    resolution: str,
) -> Dict[str, Any]:
    """
    Approve or reject a pending goal_approval tied to a plan step. Returns payload for JOB_QUEUED
    when approved (caller appends events), or status dict when rejected.
    """
    res = (resolution or "").strip().lower()
    if res not in {"approved", "rejected", "denied"}:
        return {"ok": False, "error": "resolution must be approved or rejected"}
    row = get_goal_approval(conn, approval_id)
    if not row or str(row.get("task_id") or "") != task_id:
        return {"ok": False, "error": "unknown approval for task"}
    if str(row.get("status") or "") != "pending":
        return {"ok": False, "error": "approval not pending"}

    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    plan_id = str(meta.get("plan_id") or "")
    step_id = str(meta.get("step_id") or "")
    if res in {"rejected", "denied"}:
        update_goal_approval_status(conn, approval_id, "rejected", rationale_patch="user_rejected")
        if plan_id:
            update_execution_plan(
                conn,
                plan_id,
                status=PlanStatus.BLOCKED.value,
                approval_state="rejected",
                summary_patch={"blocked_reason": "approval_rejected"},
            )
        if step_id:
            update_plan_step(conn, step_id, status=StepStatus.BLOCKED.value)
        return {"ok": True, "task_id": task_id, "resolution": "rejected"}

    update_goal_approval_status(conn, approval_id, "approved", rationale_patch="user_approved")
    if not plan_id or not step_id:
        return {"ok": False, "error": "approval metadata missing plan_id/step_id"}

    st = get_plan_step(conn, step_id)
    action = st.get("action") if isinstance(st, dict) else {}
    if not isinstance(action, dict):
        action = {}
    pending = action.get("pending_job_payload")
    if not isinstance(pending, dict):
        return {"ok": False, "error": "missing pending_job_payload on plan step"}

    update_plan_step(conn, step_id, status=StepStatus.QUEUED.value)
    update_execution_plan(
        conn,
        plan_id,
        status=PlanStatus.QUEUED.value,
        approval_state="granted",
        current_step_id=step_id,
    )
    # Caller transitions to queued via JOB_QUEUED
    job_payload = dict(pending)
    job_payload["plan_id"] = plan_id
    job_payload["execute_step_id"] = step_id
    job_payload["approval_id"] = approval_id
    return {
        "ok": True,
        "task_id": task_id,
        "resolution": "approved",
        "job_payload": job_payload,
        "plan_id": plan_id,
        "execute_step_id": step_id,
    }


def bind_step_to_attempt(conn: sqlite3.Connection, step_id: str, attempt_id: str) -> None:
    if not step_id or not attempt_id:
        return
    st = get_plan_step(conn, step_id)
    if not st:
        return
    pid = str(st.get("plan_id") or "")
    update_plan_step(
        conn,
        step_id,
        status=StepStatus.EXECUTING.value,
        execution_attempt_id=attempt_id,
    )
    if pid:
        update_execution_plan(
            conn,
            pid,
            status=PlanStatus.EXECUTING.value,
            current_step_id=step_id,
        )


def _bounded_collaboration_for_verification(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    plan_id: str,
    execute_step_id: str,
    summ: Dict[str, Any],
    sid: str,
    contract: Any,
    trigger: str,
    verdict: str,
    method: str,
    outcome_summary: str,
    lane: str,
    pr_url: str,
    agent_url: str,
) -> Tuple[
    Dict[str, Any],
    Dict[str, Any],
    Optional[Dict[str, Any]],
    str,
    List[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    """
    Persist collaboration/repair metadata for enabled scenarios.
    Returns (summary_patch, recovery_patch, collaboration_event_payload, user_note,
             collaboration_role_events, collaboration_repair_dispatch,
             activation_event_payload, collaboration_outcome_event_payload,
             repair_outcome_event_payload).
    Empty patches / no payload when disabled or inapplicable.
    """
    goal_id = str(get_goal_id_for_task(conn, task_id) or "")
    attach_blockers = explain_collaboration_attachment_blockers(
        scenario_id=sid, contract=contract, trigger=trigger, plan_summary=summ
    )
    will_attach = not bool(attach_blockers)

    if (
        collab_policy_recording_enabled()
        and str(sid or "").strip() in MEASURED_SCENARIOS
        and trigger in ("verify_fail", "trust_gate", "verify_weak")
        and not will_attach
    ):
        act = evaluate_activation_policy(
            conn=conn,
            task_id=task_id,
            plan_id=plan_id,
            step_id=execute_step_id,
            scenario_id=str(sid or ""),
            trigger=trigger,
            verdict=verdict,
            lane=lane,
            collab_id="",
            collaboration_layer_on=collaboration_layer_enabled(),
            will_attach_collaboration_bundle=False,
            attach_blocked_reasons=attach_blockers,
            base_live_advisory_eligible=False,
            approval_blocked=False,
        )
        try:
            from .assistant_followthrough import merge_followthrough_collaboration_gate

            act = merge_followthrough_collaboration_gate(
                conn, act, task_id=task_id, scenario_id=str(sid or "")
            )
        except Exception:
            pass
        act["recorded_at"] = time.time()
        try:
            insert_collaboration_activation_decision(conn, task_id, act)
        except sqlite3.OperationalError:
            pass
        return {}, {}, None, "", [], None, act, None, None

    bundle = build_collaboration_bundle(
        task_id=task_id,
        goal_id=goal_id,
        plan_id=plan_id,
        step_id=execute_step_id,
        scenario_id=sid,
        contract=contract,
        trigger=trigger,
        verdict=verdict,
        verification_method=method,
        summary=outcome_summary,
        lane=lane,
        plan_summary=summ,
        pr_url=pr_url,
        agent_url=agent_url,
    )
    if not bundle:
        return {}, {}, None, "", [], None, None, None, None
    req, repair, arb, roles, contrib = bundle
    role_events: List[Dict[str, Any]] = []
    repair_dispatch: Optional[Dict[str, Any]] = None
    live_round: Optional[Dict[str, Any]] = None
    usefulness_status = ""
    advisory_source = "deterministic"
    role_delta = 0
    act_sp: Dict[str, Any] = {}
    act_rec: Dict[str, Any] = {}
    proof_requirements = ""
    if contract is not None:
        proof_requirements = str(getattr(contract, "proof_class", "") or "")

    base_live = collaboration_runtime.advisory_live_roles_eligible(
        scenario_id=sid, trigger=trigger, plan_summary=summ
    )
    activation_event_payload: Optional[Dict[str, Any]] = None
    if str(sid or "").strip() in MEASURED_SCENARIOS:
        act_decision = evaluate_activation_policy(
            conn=conn,
            task_id=task_id,
            plan_id=plan_id,
            step_id=execute_step_id,
            scenario_id=str(sid or ""),
            trigger=trigger,
            verdict=verdict,
            lane=lane,
            collab_id=req.collab_id,
            collaboration_layer_on=collaboration_layer_enabled(),
            will_attach_collaboration_bundle=True,
            attach_blocked_reasons=[],
            base_live_advisory_eligible=base_live,
            approval_blocked=False,
        )
        try:
            from .assistant_followthrough import merge_followthrough_collaboration_gate

            act_decision = merge_followthrough_collaboration_gate(
                conn, act_decision, task_id=task_id, scenario_id=str(sid or "")
            )
        except Exception:
            pass
        act_decision["recorded_at"] = time.time()
        try:
            insert_collaboration_activation_decision(conn, task_id, act_decision)
        except sqlite3.OperationalError:
            pass
        activation_event_payload = act_decision
        run_live = bool(act_decision.get("executed_live_advisory_planned"))
    else:
        act_decision = {
            "activation_mode": "",
            "policy_version": "",
            "reason_codes": [],
        }
        run_live = base_live

    collaboration_outcome_event_payload: Optional[Dict[str, Any]] = None
    repair_outcome_event_payload: Optional[Dict[str, Any]] = None

    if run_live:
        merged = collaboration_runtime.run_collaboration_round(
            task_id=task_id,
            plan_id=plan_id,
            collab_id=req.collab_id,
            scenario_id=sid,
            trigger=trigger,
            verdict=verdict,
            outcome_summary=outcome_summary,
            lane=lane,
            deterministic_strategy=repair.strategy,
            candidate_lanes=list(req.candidate_lanes or []),
            pr_url=pr_url,
            agent_url=agent_url,
            proof_requirements=proof_requirements,
            repair=repair,
            arbitration=arb,
        )
        if merged:
            repair = merged["repair"]
            arb = merged["arbitration"]
            role_events = list(merged.get("role_events") or [])
            live_round = merged.get("live_round") if isinstance(merged.get("live_round"), dict) else None
            usefulness_status = str(merged.get("usefulness_status") or "")
            advisory_source = "live_advisory"
            role_delta = int(merged.get("role_invocation_delta") or 0)
            target_hint = str(merged.get("target_lane_hint") or "")
            act_sp, act_rec, repair_dispatch = collaboration_runtime.maybe_execute_bounded_collaboration_action(
                conn=conn,
                scenario_id=sid,
                task_id=task_id,
                plan_id=plan_id,
                collab_id=req.collab_id,
                final_strategy=repair.strategy,
                lane=lane,
                candidate_lanes=list(req.candidate_lanes or []),
                target_lane_hint=target_hint,
                plan_summary=summ,
                outcome_summary=outcome_summary,
                trigger=trigger,
            )

    c_use = canonical_usefulness_class(usefulness_status) if usefulness_status else ""
    act_mode = str((act_decision or {}).get("activation_mode") or "")
    act_ver = str((act_decision or {}).get("policy_version") or "")
    act_reasons = list((act_decision or {}).get("reason_codes") or [])

    sp = collaboration_summary_patch(
        summ,
        request=req,
        repair=repair,
        arbitration=arb,
        role_invocation_delta=role_delta,
        usefulness_status=usefulness_status,
        advisory_source=advisory_source,
        activation_mode=act_mode if str(sid or "").strip() in MEASURED_SCENARIOS else "",
        canonical_usefulness=c_use if str(sid or "").strip() in MEASURED_SCENARIOS else "",
        activation_policy_version=act_ver if str(sid or "").strip() in MEASURED_SCENARIOS else "",
        activation_reason_codes=act_reasons if str(sid or "").strip() in MEASURED_SCENARIOS else None,
    )
    if act_sp and isinstance(act_sp.get("collaboration"), dict):
        c = sp.setdefault("collaboration", {})
        for k, v in act_sp["collaboration"].items():
            c[k] = v

    recovery = {
        "collaboration_last": {
            "collab_id": req.collab_id,
            "trigger": trigger,
            "request": req.to_dict(),
            "repair": repair.to_dict(),
            "arbitration": arb.to_dict(),
        }
    }
    if live_round:
        recovery["collaboration_last"]["live_round"] = live_round
    if act_rec:
        recovery.update(act_rec)

    executed_action = (
        act_sp.get("collaboration", {}).get("last_executed_action")
        if isinstance(act_sp.get("collaboration"), dict)
        else None
    )
    _had_action = bool(
        isinstance(executed_action, dict) and str(executed_action.get("type") or "").strip()
    )
    evt = collaboration_event_payload(
        task_id=task_id,
        plan_id=plan_id,
        step_id=execute_step_id,
        scenario_id=sid,
        request=req,
        repair=repair,
        arbitration=arb,
        role_assignments=roles,
        contribution=contrib,
        live_round=live_round,
        advisory_only=not _had_action,
        executed_action=executed_action if isinstance(executed_action, dict) else None,
        usefulness_status=usefulness_status,
    )
    note = collaboration_repair_user_note(strategy=repair.strategy, proof_plan=repair.proof_plan)

    bounded_action_type = ""
    if isinstance(executed_action, dict):
        bounded_action_type = str(executed_action.get("type") or "")

    if str(sid or "").strip() in MEASURED_SCENARIOS:
        collaboration_outcome_event_payload = build_collaboration_outcome_payload(
            task_id=task_id,
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=execute_step_id,
            collab_id=req.collab_id,
            scenario_id=sid,
            trigger=trigger,
            verdict_before=verdict,
            verification_method=method,
            advisory_source=advisory_source,
            usefulness_detail=usefulness_status,
            final_strategy=repair.strategy,
            bounded_action_type=bounded_action_type,
            live_advisory_ran=bool(run_live),
            role_invocation_delta=role_delta,
            policy_version=act_ver or ACTIVATION_POLICY_VERSION,
        )
        try:
            insert_collaboration_outcome_row(conn, task_id, collaboration_outcome_event_payload)
            evaluate_promotion_guardrails_after_outcome(
                conn,
                scenario_id=sid,
                trigger=trigger,
                canonical_class=str(collaboration_outcome_event_payload.get("canonical_class") or ""),
            )
        except sqlite3.OperationalError:
            pass

    if _had_action and isinstance(executed_action, dict) and str(sid or "").strip() in MEASURED_SCENARIOS:
        dispatch_kind = ""
        if isinstance(repair_dispatch, dict):
            dispatch_kind = str(repair_dispatch.get("kind") or "")
        repair_outcome_event_payload = build_repair_outcome_payload(
            task_id=task_id,
            plan_id=plan_id,
            collab_id=req.collab_id,
            action_type=str(executed_action.get("type") or ""),
            executed=True,
            from_lane=str(executed_action.get("from_lane") or executed_action.get("lane") or lane),
            to_lane=str(executed_action.get("to_lane") or ""),
            dispatch_kind=dispatch_kind,
            verdict_after="",
        )
        try:
            insert_repair_outcome_row(conn, task_id, repair_outcome_event_payload)
        except sqlite3.OperationalError:
            pass

    return (
        sp,
        recovery,
        evt,
        note,
        role_events,
        repair_dispatch,
        activation_event_payload,
        collaboration_outcome_event_payload,
        repair_outcome_event_payload,
    )


def finalize_execute_step_verification(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    plan_id: str,
    execute_step_id: str,
    terminal_status: str,
    pr_url: str = "",
    agent_url: str = "",
    lane: str = "",
) -> Dict[str, Any]:
    """
    Record verification for the execute_delegated step after terminal Cursor status.
    Returns dict with keys: verdict, should_complete_job (bool), verification_id, summary, recovery_suggestions
    """
    step = get_plan_step(conn, execute_step_id)
    if not step:
        return {"verdict": "pass", "should_complete_job": True, "summary": "no plan step", "recovery_suggestions": []}

    plan_row = get_execution_plan(conn, plan_id)
    summ = plan_row.get("summary") if isinstance(plan_row, dict) else {}
    if not isinstance(summ, dict):
        summ = {}
    proof_class = str(summ.get("proof_class") or "")
    method = verification_method_for_scenario(
        lane,
        str(step.get("step_kind") or StepKind.EXECUTE_DELEGATED.value),
        proof_class,
    )
    outcome = evaluate_delegated_repo_outcome(
        terminal_status=terminal_status,
        pr_url=pr_url,
        agent_url=agent_url,
        lane=lane,
        verification_method=method,
    )
    vid = new_verification_id()
    insert_verification_result(
        conn,
        vid,
        plan_id=plan_id,
        step_id=execute_step_id,
        method=method,
        verdict=str(outcome.get("verdict") or "fail"),
        summary=str(outcome.get("summary") or ""),
        evidence=outcome.get("evidence") if isinstance(outcome.get("evidence"), dict) else {},
    )

    verdict = str(outcome.get("verdict") or "fail")
    recovery_suggestions = [{"action": a, "detail": d} for a, d in suggest_recovery("policy")]

    sid = str(summ.get("scenario_id") or "").strip()
    contract = get_contract(sid) if sid else None
    has_proof = proof_signals_satisfied_for_trusted_completion(
        verification_verdict=verdict,
        verification_method=method,
        pr_url=pr_url,
        agent_url=agent_url,
    )

    def _scenario_receipt_lines(*, verified: bool) -> str:
        if not sid:
            return ""
        label = (contract.user_facing_label if contract else "") or sid
        return format_scenario_proof_receipt(
            scenario_id=sid,
            scenario_label=label,
            verified=verified,
            proof_summary=str(outcome.get("summary") or ""),
            next_step=scenario_verification_footer(proof_class=str(summ.get("proof_class") or "")),
        )

    def _trust_allows_completion() -> bool:
        if not contract:
            return True
        return trusted_receipt_allowed(
            contract,
            verification_verdict=verdict,
            has_required_proof=has_proof,
        )

    weak_complete = verdict == "needs_human" and not strict_post_execution_verification()
    if (verdict == "pass" or weak_complete) and not _trust_allows_completion():
        rx = _scenario_receipt_lines(verified=False)
        (
            c_sp,
            c_rec,
            c_evt,
            c_note,
            c_roles,
            c_dispatch,
            c_act,
            c_out,
            c_rep,
        ) = _bounded_collaboration_for_verification(
            conn,
            task_id=task_id,
            plan_id=plan_id,
            execute_step_id=execute_step_id,
            summ=summ,
            sid=sid,
            contract=contract,
            trigger="trust_gate",
            verdict=verdict,
            method=method,
            outcome_summary=str(outcome.get("summary") or ""),
            lane=lane,
            pr_url=pr_url,
            agent_url=agent_url,
        )
        recov_tg = {
            "suggestions": recovery_suggestions,
            "last_verdict": verdict,
            "trust_gate": True,
        }
        recov_tg.update(c_rec)
        summ_patch_tg = {
            "verification_failure": str(outcome.get("summary") or ""),
            "receipt_state": "blocked_trust",
        }
        summ_patch_tg.update(c_sp)
        update_plan_step(
            conn,
            execute_step_id,
            status=StepStatus.VERIFYING.value,
            recovery_patch=recov_tg,
        )
        update_execution_plan(
            conn,
            plan_id,
            status=PlanStatus.BLOCKED.value,
            verification_state="failed:trust_gate",
            recovery_state="suggested",
            summary_patch=summ_patch_tg,
        )
        out_tg: Dict[str, Any] = {
            "verdict": verdict,
            "should_complete_job": False,
            "verification_id": vid,
            "summary": str(outcome.get("summary") or ""),
            "recovery_suggestions": recovery_suggestions,
            "receipt_state": "blocked_trust",
            "scenario_user_receipt": rx,
        }
        if c_evt:
            out_tg["collaboration_event_payload"] = c_evt
        if c_note:
            out_tg["collaboration_user_note"] = c_note
        if c_roles:
            out_tg["collaboration_role_events"] = c_roles
        if c_dispatch:
            out_tg["collaboration_repair_dispatch"] = c_dispatch
        if c_act:
            out_tg["activation_event_payload"] = c_act
        if c_out:
            out_tg["collaboration_outcome_event_payload"] = c_out
        if c_rep:
            out_tg["repair_outcome_event_payload"] = c_rep
        return out_tg

    if verdict == "pass":
        update_plan_step(
            conn,
            execute_step_id,
            status=StepStatus.COMPLETED.value,
            result_patch={"verification_id": vid, "verdict": verdict},
        )
        update_execution_plan(
            conn,
            plan_id,
            status=PlanStatus.VERIFYING.value,
            verification_state="passed_execute",
            current_step_id=execute_step_id,
            summary_patch={"receipt_state": "verified"},
        )
        return {
            "verdict": verdict,
            "should_complete_job": True,
            "verification_id": vid,
            "summary": str(outcome.get("summary") or ""),
            "recovery_suggestions": [],
            "receipt_state": "verified",
            "scenario_user_receipt": _scenario_receipt_lines(verified=True),
        }

    if verdict == "needs_human" and not strict_post_execution_verification():
        update_plan_step(
            conn,
            execute_step_id,
            status=StepStatus.COMPLETED.value,
            result_patch={
                "verification_id": vid,
                "verdict": verdict,
                "weak_verification": True,
            },
        )
        update_execution_plan(
            conn,
            plan_id,
            verification_state="weak_pass",
            summary_patch={
                "verification_note": outcome.get("summary"),
                "receipt_state": "verified_weak",
            },
        )
        return {
            "verdict": verdict,
            "should_complete_job": True,
            "verification_id": vid,
            "summary": str(outcome.get("summary") or ""),
            "recovery_suggestions": recovery_suggestions,
            "receipt_state": "verified_weak",
            "scenario_user_receipt": _scenario_receipt_lines(verified=False),
        }

    (
        c_sp_f,
        c_rec_f,
        c_evt_f,
        c_note_f,
        c_roles_f,
        c_dispatch_f,
        c_act_f,
        c_out_f,
        c_rep_f,
    ) = _bounded_collaboration_for_verification(
        conn,
        task_id=task_id,
        plan_id=plan_id,
        execute_step_id=execute_step_id,
        summ=summ,
        sid=sid,
        contract=contract,
        trigger="verify_fail",
        verdict=verdict,
        method=method,
        outcome_summary=str(outcome.get("summary") or ""),
        lane=lane,
        pr_url=pr_url,
        agent_url=agent_url,
    )
    recov_fail = {"suggestions": recovery_suggestions, "last_verdict": verdict}
    recov_fail.update(c_rec_f)
    summ_patch_fail = {
        "verification_failure": str(outcome.get("summary") or ""),
        "receipt_state": "blocked",
    }
    summ_patch_fail.update(c_sp_f)
    update_plan_step(
        conn,
        execute_step_id,
        status=StepStatus.VERIFYING.value,
        recovery_patch=recov_fail,
    )
    fail_rx = _scenario_receipt_lines(verified=False)
    update_execution_plan(
        conn,
        plan_id,
        status=PlanStatus.BLOCKED.value,
        verification_state=f"failed:{verdict}",
        recovery_state="suggested",
        summary_patch=summ_patch_fail,
    )
    out: Dict[str, Any] = {
        "verdict": verdict,
        "should_complete_job": False,
        "verification_id": vid,
        "summary": str(outcome.get("summary") or ""),
        "recovery_suggestions": recovery_suggestions,
        "receipt_state": "blocked",
    }
    if fail_rx:
        out["scenario_user_receipt"] = fail_rx
    if c_evt_f:
        out["collaboration_event_payload"] = c_evt_f
    if c_note_f:
        out["collaboration_user_note"] = c_note_f
    if c_roles_f:
        out["collaboration_role_events"] = c_roles_f
    if c_dispatch_f:
        out["collaboration_repair_dispatch"] = c_dispatch_f
    if c_act_f:
        out["activation_event_payload"] = c_act_f
    if c_out_f:
        out["collaboration_outcome_event_payload"] = c_out_f
    if c_rep_f:
        out["repair_outcome_event_payload"] = c_rep_f
    return out


def record_verification_event_payload(
    *,
    plan_id: str,
    step_id: str,
    verification_id: str,
    verdict: str,
    summary: str,
    method: str = "",
) -> Dict[str, Any]:
    return {
        "plan_id": plan_id,
        "step_id": step_id,
        "verification_id": verification_id,
        "verdict": verdict,
        "summary": summary[:2000] if summary else "",
        "method": method or "",
    }
