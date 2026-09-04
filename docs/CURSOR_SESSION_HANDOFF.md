# Cursor session handoff snapshot

Last updated: 2026-09-04 offline doctor machine-receipt product pass.

## Current draft handoff

- **Base:** exact `main` `58f63ca65b95805c109cd07b5d741397bbdbcf01` (merged #17).
- **Product change:** the existing operator-testable offline doctor can also
  write one mode-`600`, fingerprinted JSON receipt for Bob, Codex, Grok,
  Claude, dashboards, and scripts. It carries allowlisted stage outcomes and
  the same Andrea / coding-agent / owner action contract; raw probe output,
  environment values, capability notes, and checkout paths are absent. Grade
  C or any failed stage is always `blocked`. Human output remains unchanged.
- **Fence:** `OUTBOUND_CONFIRM_RE` is unchanged. `services/andrea_sync/server.py`
  blob `8c5efa82` is identical to base. `conversation_eval` is unchanged.
- **Operator path:** `bash scripts/andrea_doctor.sh --offline --receipt
  /tmp/andrea-doctor-receipt.json`, then read `--- Operator next steps ---`
  and hand the JSON artifact to the next agent. The receipt option refuses to
  run without `--offline`.
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
