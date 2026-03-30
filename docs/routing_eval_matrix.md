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
- `routing_matrix::opinion_reflection::rm_personality_feedback_voice`
- `routing_matrix::casual_conversation::rm_collaborative_day_plan`

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
| **Personality / tone feedback** | Meta prompts (e.g. “show more personality”, “trying to be funny”) match `_PERSONALITY_FEEDBACK_RE` → `lightweight_conversational_kind` **`personality_feedback`**: direct lane, **no** grounded lookup ([`server.py`](services/andrea_sync/server.py) skip), warmer heuristic + optional direct LLM polish ([`andrea_router.py`](services/andrea_sync/andrea_router.py)). |
| **Collaborative day plan** | Assistant-directed “what do **you** want to do today / what should **we** do” matches `_COLLABORATIVE_DAY_PLAN_RE` → **`collaborative_day_plan`**: same lightweight + no-lookup treatment (distinct from user **calendar** agenda patterns in `_AGENDA_RE`). |
| **Structured outbound SMS vs OpenClaw delegation** | Telegram [`extract_routing_hints`](services/andrea_sync/adapters/telegram.py) replaces `@andrea` / `@openclaw` / `@cursor` with a space, so `Tell @openclaw to …` becomes `Tell to …`. The outbound `tell <target> <body>` pattern in [`server.py`](services/andrea_sync/server.py) must **not** treat the infinitive **to** as a recipient (`OUTBOUND_INVALID_TARGETS`). Otherwise structured handling drafts an SMS (`outbound_message_drafted`) **before** delegation. Follow-up lines that mention a **to-do / todo list** clear a mistaken pending draft (`OUTBOUND_DRAFT_TODO_CLARIFICATION_RE`) so the turn can continue without requiring exact `cancel`. |

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
- Personality feedback (`rm_personality_feedback_voice`)
- Collaborative what-to-do-today (`rm_collaborative_day_plan`)
- Personal iMessage / **BlueBubbles** lane (capability ask, recent-text fetch, outbound draft) is documented and enforced at the OpenClaw layer; see [ANDREA_OPENCLAW_HYBRID_SKILLS.md](ANDREA_OPENCLAW_HYBRID_SKILLS.md) and patched harness helpers in [`services/andrea_sync/conversation_eval.py`](services/andrea_sync/conversation_eval.py) (e.g. `patch_bluebubbles`). Phase A matrix rows can grow explicit `routing_matrix::…` cases when the eval environment stubs BlueBubbles consistently.
- Tell `@openclaw` to add a to-do item — must **not** become structured SMS draft (`outbound_message_drafted`); regression tests in [`tests/test_andrea_sync.py`](tests/test_andrea_sync.py) (`test_parse_outbound_rejects_tell_to_after_stripped_openclaw_mention`, `test_server_followups_tell_openclaw_todo_routing_text_not_outbound_draft`, `test_server_clears_outbound_draft_on_todo_list_clarification`). A dedicated `routing_matrix::…` harness case is deferred until the experience environment consistently delegates this utterance (today it may still surface generic direct clarification copy).

Phase B can add continuation-attachment cases, mixed bundles, and grounded/news variants.

`conversation_core` also includes **`personality_feedback_more_voice`** and **`collaborative_day_plan_assistant**` for the same prompt families without the `routing_matrix::` prefix.
