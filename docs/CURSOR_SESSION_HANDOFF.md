# Cursor session handoff snapshot

Last updated: 2026-09-03 Codex readiness-action-plan product draft (unmerged).

## Current draft handoff

- **Base:** exact `main` `5d7d0daa01e669d6a3b928eece7a5a803a98724e`
- **Branch:** `codex/cursor-openclaw-product-20260903`
- **Product change:** `andrea_readiness_grade.py` now emits one prioritized,
  code-owned next action plus a redaction-safe `readiness_plan` contract that
  humans, Codex, Grok, Claude, dashboards, and scripts can share. Probe notes
  and secret values are never copied into the plan.
- **Operator path:** `bash scripts/andrea_doctor.sh --offline` is the simple
  no-live-model-probe form. Unknown doctor options now fail instead of being
  silently ignored.
- **Verification:** 752 offline tests, 9 vendored handoff-skill tests, all 11
  integration-gate stages, security sanity, reliability probes, exhaustive
  CLI validation, shell syntax, Python compile, and diff integrity passed.
- **Host discovery:** the current Mac reports Grade C because four critical
  OpenClaw skills are not detected. The plan correctly puts the checked-in
  `cursor_handoff` install guidance first. This draft did not install skills,
  alter credentials/settings, contact a provider, or restart the gateway.
- **Remaining:** Karen leftover+security review, then Jeff decides any merge
  and separately owns any skill installation or gateway restart.

## Earlier shipped snapshot

## Git

- **Branch:** `main`
- **Feature commit:** `17ef40f` — `fix(sync): block casual greetings from Telegram continuation + social route` (see `git log` for any follow-up docs commits on `main`)
- **Remote:** pushed to `origin/main`

## What shipped

- `is_standalone_casual_social_turn()` in `services/andrea_sync/andrea_router.py`: short greetings + `CASUAL_CHECKIN_RE` check-ins stay direct/social.
- `classify_route()` uses that helper (not only `_is_greeting_only` + word cap).
- `services/andrea_sync/telegram_continuation.py`: those turns do **not** attach to an active Telegram task (avoids `format_final_message` / task-summary surface for casual text).

## Tests run (targeted)

```bash
python3 -m pytest \
  tests/test_andrea_sync.py::TestAndreaSync::test_telegram_continuation_hi_andrea_does_not_merge_queued_collab_task \
  tests/test_andrea_sync.py::TestAndreaSync::test_telegram_continuation_good_morning_andrea_does_not_merge_queued_collab_task \
  tests/test_andrea_sync.py::TestAndreaSync::test_classify_route_casual_checkin_is_greeting_or_social \
  tests/test_andrea_sync.py::TestAndreaSync::test_is_standalone_casual_social_turn_covers_planned_phrases \
  tests/test_andrea_sync.py::TestAndreaSync::test_server_followups_route_hows_it_going_greeting_or_social \
  tests/test_andrea_sync.py::TestAndreaSync::test_server_followups_plain_hi_andrea_direct_without_task_summary_surface \
  tests/test_andrea_sync_http.py::TestAndreaSyncHTTPWebhookHeader::test_telegram_plain_greeting_after_queued_collab_is_new_direct_task \
  -q
```

Result: **7 passed** (re-run after commit/push).

## Runtime (this host)

- `bash scripts/andrea_services.sh restart sync` was run so the Andrea sync process reloads repo code.
- **Live smoke (operator):** in Telegram, try `Hi Andrea`, `Good morning Andrea`, `How's it going?` (and smart apostrophe variant) especially when another task is active in the same chat; replies should stay conversational, not task/failure summaries.

## Optional triage for the next agent

Repo includes `scripts/handoff_context.py` (used by `cursor_handoff`); you can generate pre-handoff git triage from that tooling if needed.
