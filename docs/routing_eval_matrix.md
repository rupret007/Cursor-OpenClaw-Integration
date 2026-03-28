# Telegram routing evaluation matrix

Structured scenarios for measuring **Telegram user text → routing → assistant surface** quality.  
Suite id: `routing_matrix` (see `run_experience_assurance(..., suite="routing_matrix")`).

## Case ID scheme

```
routing_matrix::<behavior_family>::<variant>
```

Examples:

- `routing_matrix::casual_conversation::rm_casual_hows`
- `routing_matrix::meta_stack::rm_meta_openclaw_whats_up`
- `routing_matrix::control_plane::rm_control_cancel_no_jobs`
- `routing_matrix::casual_conversation::rm_math_then_which_clarification`

## Dimensions (taxonomy)

| Dimension | Values / notes |
|-----------|----------------|
| **Turn shape** | Short social, question, imperative, multi-turn |
| **Mentions** | none, `@openclaw`, `@cursor`, `Ask @cursor …` |
| **Principal state** | default harness principal; optional `setup_fn` for goals/reminders |
| **Environment** | Calendar JSON env, OpenClaw stubs, `mock_cancel_all_jobs`, BlueBubbles patches (via `ConversationCaseSpec` flags) |
| **Wait policy** | `terminal_reply` (default) vs `routing_smoke` (allows queued/running) |
| **Stack / placement** | Questions like “is that in OpenClaw or Andrea?” are classified as **tooling identity** in [`turn_intelligence.py`](services/andrea_sync/turn_intelligence.py) (`is_tooling_identity_question`) so they stay **lightweight direct** and avoid grounded-research “next steps” tails. |
| **Anaphoric clarification** | Ultra-short lines such as “Which is what?” / “What’s that?” match `_BARE_DIALOGUE_CLARIFICATION_RE` in [`turn_intelligence.py`](services/andrea_sync/turn_intelligence.py) so they are **lightweight conversational** (not substantive / lookup-eligible). [`andrea_router.py`](services/andrea_sync/andrea_router.py) routes them as **`lightweight_followup_direct`** and answers from **recent assistant history** when possible. |
| **Apostrophe normalization** | Classification normalizes Unicode apostrophes (`’`, backtick) to ASCII in [`turn_intelligence.py`](services/andrea_sync/turn_intelligence.py) so mobile “What’s that?” matches the same patterns as a straight-quote spelling. |

## Data capture

Each turn emits a **routing capture** row (see `metadata["routing_captures"]` on the check result):

- `case_id`, `scenario_id`, `behavior_family`, `capture_tags`, `turn_index`
- `user_text`, `assistant_route`, `assistant_reason`, `task_status`
- `turn_plan_domain`, `turn_plan_continuity_focus`
- `execution_lane`, `event_type_counts`, `harness_timing`

Optional export: set env **`ANDREA_ROUTING_EVAL_EXPORT`** to a file path; one **NDJSON** line is appended per turn (for notebooks / BI).

## Routing contracts (optional per case)

Fields on `ConversationCaseSpec` (see `conversation_eval.py`):

- `routing_expected_assistant_route` — exact `assistant.route` on the contract turn
- `routing_expect_delegate` — `True` / `False` / unset; checks delegate-shaped outcomes
- `routing_required_execution_lane_substr` — substring required in `meta.execution.lane`
- `routing_expect_intent_control_plane` / `routing_expect_intent_family` — `classify_intent_envelope` checks
- `routing_contract_turn_index` — which turn to evaluate (`-1` = last)

## Running

```bash
# Full matrix
python3 scripts/andrea_experience_cycle.py --suite routing_matrix

# Subset (same options as conversation_core)
python3 scripts/andrea_experience_cycle.py --suite routing_matrix --scenario-ids rm_casual_hows,rm_openclaw_schedule_stub

# Smoke subset (fast CI)
python3 scripts/andrea_experience_cycle.py --suite routing_matrix --smoke

# Export captures
ANDREA_ROUTING_EVAL_EXPORT=./artifacts/routing_captures.ndjson python3 scripts/andrea_experience_cycle.py --suite routing_matrix --smoke
```

## Phase A coverage (initial catalog)

- Casual / check-in (no generic fallback leak)
- OpenClaw meta pings (casual wording)
- Agenda / calendar visibility (`calendar` in empty-state copy)
- Explicit `@openclaw` schedule with calendar stub
- Control plane cancel (stubbed CLI) + intent flags
- Greeting then agenda (multi-turn)
- Math then anaphoric “which is what?” (multi-turn; contract on final turn)

Phase B can add continuation-attachment cases, mixed bundles, and grounded/news variants.
