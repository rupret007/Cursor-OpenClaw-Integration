# Cursor session handoff snapshot

Last updated: 2026-09-04 offline doctor operator-testable draft.

## Current draft handoff

- **Base:** exact `main` `494ccf8a4bda6fb6952d6d33965bb953c411c651` (merged leftover #16).
- **Product change:** offline doctor / readiness is operator-testable. Human
  grade output leads with a marked recap (`Who acts first` plus Next for
  Andrea / Bob / owner). `bash scripts/andrea_doctor.sh --offline` continues
  through probes on Grade C, reprints that recap, and is the command README
  and the playbook tell an operator to run. A unit test executes that exact
  command. Live doctor still fail-closes immediately on Grade C.
- **Fence:** `OUTBOUND_CONFIRM_RE` is unchanged. `services/andrea_sync/server.py`
  blob `8c5efa82` is identical to base. `conversation_eval` is unchanged.
- **Operator path:** `bash scripts/andrea_doctor.sh --offline` then read
  `--- Operator next steps ---`.
- **Holds:** no live send, Private API off, no BlueBubbles live send, no
  credential writes, no merge/tag/deploy/gateway restart unless the owner asks.

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
