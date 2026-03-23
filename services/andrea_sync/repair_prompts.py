"""Centralized prompts for the incident-driven repair lanes."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from .repair_types import Incident, PatchAttempt, RepairPlan, VerificationCheck

REPAIR_PROMPT_VERSION = "v1"
REPAIR_JSON_MARKER = "REPAIR_JSON:"


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _render_lines(items: Iterable[Any], *, bullet: str = "- ", empty: str = "- none") -> str:
    rendered: List[str] = []
    for raw in items:
        text = _clip(raw, 500)
        if text:
            rendered.append(f"{bullet}{text}")
    return "\n".join(rendered) or empty


def _render_context_files(context_files: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for item in context_files[:6]:
        path = str(item.get("path") or "").strip()
        content = str(item.get("content") or "").rstrip()
        if not path or not content:
            continue
        blocks.append(f"[file] {path}\n{content}")
    return "\n\n".join(blocks) or "[file] none"


def _verification_summary(report: Dict[str, Any]) -> str:
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    lines: List[str] = []
    for check in checks[:8]:
        if not isinstance(check, dict):
            continue
        label = str(check.get("label") or check.get("check_id") or "check")
        status = "passed" if bool(check.get("passed")) else "failed"
        excerpt = _clip(check.get("output_excerpt") or check.get("stderr") or check.get("stdout") or "", 400)
        if excerpt:
            lines.append(f"- {label}: {status} :: {excerpt}")
        else:
            lines.append(f"- {label}: {status}")
    return "\n".join(lines) or "- no verification results recorded"


def _build_json_contract(schema: Dict[str, Any]) -> str:
    return (
        f"Return exactly one single-line marker in this format:\n"
        f"{REPAIR_JSON_MARKER} {json.dumps(schema, ensure_ascii=False)}\n"
        "Do not wrap the marker in a code block.\n"
        "Do not add any JSON keys beyond the schema unless they are clearly useful and safe.\n"
    )


def build_triage_prompt(
    *,
    incident: Incident,
    verification_report: Dict[str, Any],
    recent_diff_summary: str,
    budget_state: Dict[str, Any],
) -> str:
    schema = {
        "summary": "",
        "classification": "",
        "probable_root_cause": "",
        "affected_files": [],
        "failing_tests": [],
        "relevant_stack_lines": [],
        "recent_diff_summary": "",
        "recommended_repair_scope": "",
        "confidence": 0.0,
        "safe_to_auto_attempt": False,
        "needs_human_review": False,
    }
    return (
        f"You are Andrea's incident triage lane. Prompt version: {REPAIR_PROMPT_VERSION}.\n"
        "Classify the failure, compress it aggressively, and estimate the smallest safe repair scope.\n"
        "Use the smallest relevant context only. If confidence is low or the issue looks unsafe, say so.\n"
        "Valid classifications:\n"
        "- config_issue\n"
        "- dependency_issue\n"
        "- code_bug\n"
        "- flaky_test\n"
        "- infra_issue\n"
        "- data_contract_issue\n"
        "- unclear_or_unsafe\n\n"
        f"{_build_json_contract(schema)}"
        "Rules:\n"
        "- Confidence must be a float from 0.0 to 1.0.\n"
        "- safe_to_auto_attempt should be true only if a narrow low-risk repair looks realistic.\n"
        "- recommended_repair_scope should stay concrete, e.g. '1-3 files in services/andrea_sync/'.\n"
        "- affected_files should be repo-relative when possible.\n\n"
        f"Incident summary:\n{incident.summary}\n\n"
        f"Known error type hint: {incident.error_type or 'unknown'}\n"
        f"Stack trace excerpt:\n{_clip(incident.stack_trace, 2000) or '- none'}\n\n"
        f"Failing tests:\n{_render_lines(incident.failing_tests)}\n\n"
        f"Suspected files:\n{_render_lines(incident.suspected_files)}\n\n"
        f"Recent diff summary:\n{_clip(recent_diff_summary, 1200) or '- none'}\n\n"
        f"Verification summary:\n{_verification_summary(verification_report)}\n\n"
        f"Budget state:\n{json.dumps(budget_state, ensure_ascii=False)}\n"
    )


def build_primary_patch_prompt(
    *,
    incident: Incident,
    context_files: List[Dict[str, Any]],
    attempt_number: int,
    budget_state: Dict[str, Any],
) -> str:
    schema = {
        "reasoning_summary": "",
        "files_touched": [],
        "diff": "",
        "tests_expected": [],
        "confidence": 0.0,
        "safe_to_apply": False,
        "test_change_reason": "",
    }
    return (
        f"You are Andrea's first-pass surgical patch lane. Prompt version: {REPAIR_PROMPT_VERSION}.\n"
        "Generate the smallest viable unified diff that could repair this incident.\n"
        "Prefer surgical edits. Avoid rewrites. Avoid touching unrelated files.\n\n"
        f"{_build_json_contract(schema)}"
        "Rules:\n"
        f"- This is attempt {attempt_number}; touch at most 3 files.\n"
        "- Output a valid unified diff in the diff field only. No code fences.\n"
        "- Do not modify secrets, auth, billing, migrations, or destructive database flows.\n"
        "- Do not change tests just to make them pass unless the test was clearly stale or incorrect.\n"
        "- tests_expected should name the checks that should pass after the patch.\n\n"
        f"Incident summary:\n{incident.summary}\n\n"
        f"Probable root cause:\n{incident.probable_root_cause or '- unknown'}\n\n"
        f"Recommended repair scope:\n{incident.recommended_repair_scope or '- narrow safe fix only'}\n\n"
        f"Budget state:\n{json.dumps(budget_state, ensure_ascii=False)}\n\n"
        f"Relevant files:\n{_render_context_files(context_files)}\n"
    )


def build_challenger_patch_prompt(
    *,
    incident: Incident,
    failed_attempt: PatchAttempt,
    context_files: List[Dict[str, Any]],
    attempt_number: int,
    budget_state: Dict[str, Any],
) -> str:
    schema = {
        "reasoning_summary": "",
        "files_touched": [],
        "diff": "",
        "tests_expected": [],
        "confidence": 0.0,
        "safe_to_apply": False,
        "test_change_reason": "",
        "critique_of_previous_attempt": "",
    }
    verification_excerpt = _clip(
        failed_attempt.verification_results.get("summary")
        or failed_attempt.verification_results.get("output_excerpt")
        or "",
        1200,
    )
    return (
        f"You are Andrea's challenger patch lane. Prompt version: {REPAIR_PROMPT_VERSION}.\n"
        "Critique the previous patch attempt and produce a smaller or smarter follow-up diff when safe.\n"
        "If the issue is structural, say so and keep the diff empty.\n\n"
        f"{_build_json_contract(schema)}"
        "Rules:\n"
        f"- This is attempt {attempt_number}; touch at most 5 files.\n"
        "- Keep the blast radius smaller than a full rewrite.\n"
        "- If the previous attempt failed because the root cause is broader, say so in critique_of_previous_attempt.\n"
        "- If you cannot produce a safe diff, leave diff empty and set safe_to_apply=false.\n\n"
        f"Incident summary:\n{incident.summary}\n\n"
        f"Previous attempt reasoning:\n{failed_attempt.reasoning_summary or '- none'}\n\n"
        f"Previous attempt error:\n{failed_attempt.error or '- none'}\n\n"
        f"Previous verification result:\n{verification_excerpt or '- none'}\n\n"
        f"Budget state:\n{json.dumps(budget_state, ensure_ascii=False)}\n\n"
        f"Relevant files:\n{_render_context_files(context_files)}\n"
    )


def build_deep_debug_prompt(
    *,
    incident: Incident,
    attempts: List[PatchAttempt],
    context_files: List[Dict[str, Any]],
    budget_state: Dict[str, Any],
) -> str:
    schema = {
        "root_cause": "",
        "steps": [],
        "files_to_modify": [],
        "risks": [],
        "verification_plan": [],
        "stop_conditions": [],
        "handoff_summary": "",
    }
    prior_attempts = []
    for attempt in attempts[-2:]:
        prior_attempts.append(
            f"- attempt {attempt.attempt_number} ({attempt.stage} / {attempt.model_used}): "
            f"status={attempt.status} success={attempt.success} error={_clip(attempt.error, 240)}"
        )
    return (
        f"You are Andrea's deep debugging lane. Prompt version: {REPAIR_PROMPT_VERSION}.\n"
        "Both lightweight repair attempts failed or were unsafe. Produce a repair plan for a broader but still controlled execution stage.\n\n"
        f"{_build_json_contract(schema)}"
        "Rules:\n"
        "- Diagnose root cause, define the smallest coherent multi-file plan, and call out explicit risks.\n"
        "- verification_plan should be concrete and ordered.\n"
        "- stop_conditions should say when the repair becomes too broad or unsafe.\n"
        "- handoff_summary should be concise and suitable for a Cursor execution handoff.\n\n"
        f"Incident summary:\n{incident.summary}\n\n"
        f"Probable root cause so far:\n{incident.probable_root_cause or '- unknown'}\n\n"
        f"Prior attempts:\n{chr(10).join(prior_attempts) or '- none'}\n\n"
        f"Budget state:\n{json.dumps(budget_state, ensure_ascii=False)}\n\n"
        f"Relevant files:\n{_render_context_files(context_files)}\n"
    )


def build_cursor_handoff_prompt(
    *,
    incident: Incident,
    plan: RepairPlan,
    attempts: List[PatchAttempt],
    verification_checks: List[VerificationCheck],
) -> str:
    prior_attempts = []
    for attempt in attempts[-2:]:
        prior_attempts.append(
            f"- Attempt {attempt.attempt_number} ({attempt.stage}, {attempt.model_used}): "
            f"status={attempt.status}; error={_clip(attempt.error, 240) or 'n/a'}"
        )
    verification_lines = [
        f"- {check.label}: `{check.command}`" for check in verification_checks if check.enabled
    ]
    return (
        "You are Andrea's heavy-lift Cursor execution lane for a controlled repair plan.\n"
        "Implement the plan step by step in the current repo, keep the blast radius constrained, and run verification after meaningful changes.\n"
        "Stop if the plan grows beyond the stated stop conditions.\n\n"
        f"Incident: {incident.summary}\n"
        f"Root cause: {plan.root_cause}\n\n"
        "Repair steps:\n"
        f"{_render_lines(plan.steps)}\n\n"
        "Files to modify:\n"
        f"{_render_lines(plan.files_to_modify)}\n\n"
        "Risks:\n"
        f"{_render_lines(plan.risks)}\n\n"
        "Stop conditions:\n"
        f"{_render_lines(plan.stop_conditions)}\n\n"
        "Verification plan:\n"
        f"{_render_lines(plan.verification_plan or verification_lines)}\n\n"
        "Prior lightweight attempts:\n"
        f"{chr(10).join(prior_attempts) or '- none'}\n"
    )
