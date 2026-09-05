# Cursor session handoff snapshot

Last updated: 2026-09-04 resilient operator-monitor product pass.

## Current draft handoff

- **Base:** exact `main` `8540a9d90cc062046400c65316252d5b3f731771`
  (operator-readiness #20 already merged; do not redo it).
- **Branch:** `codex/openclaw-operator-product-20260904`.
- **Product change:** the existing dashboard now has honest overview connection
  status, bounded single-flight polling, and task selection that cannot show a
  late response for the wrong task. HTTP and JSON reads time out after 10 seconds,
  including across suspended timers; the next overview poll starts 5 seconds
  after completion. Failed/malformed reads and 15-second stale overviews hide
  previous status and revoke the displayed readiness actor/action. Retry restores
  current data without any new live action or endpoint.
- **Task UX:** native keyboard buttons preserve focus across polling. Same-task
  background reads keep explicitly labeled last-received details; unchanged
  payloads avoid rebuilding them. Selection changes, empty task lists, failed
  detail reads, and overview loss clear old details. Request cancellation and
  generation/identity checks prevent stale success or failure from replacing a
  newer selection. Detail failures remain separate from overview connectivity.
- **Evidence:** executable Node fixture tests run the actual rendered JavaScript
  via Python, with fake timers and transport (including ignored aborts and hung
  JSON). Existing dashboard/HTTP tests and the full offline integration gate are
  required. Node.js 18+ is a test-only prerequisite, not a new deployed service.
- **Browser check:** synthetic, fully intercepted Chromium requests only;
  desktop and 390px mobile checked for layout, overflow, keyboard selection,
  retained focus, unavailable state, and retry. No live runtime was opened.
- **Local verification:** all 11 integration stages passed: 787 Python tests
  (84 subtests), 9 vendored skill tests, syntax/security/reliability/dry-run and
  exhaustive offline checks. The dashboard runtime test contains 21 executable
  JavaScript scenarios. Existing focused dashboard/HTTP coverage: 48 passed.
  Node syntax and `git diff --check` passed. Hosted CI is recorded on the draft
  and coordination issue; these local results do not claim a deployment.
- **Fence:** `OUTBOUND_CONFIRM_RE` is unchanged
  (`send it` / `send it now` / `send now`). `services/andrea_sync/server.py`
  blob `8c5efa82` is identical to base. Private API stays off.
- **Operator path:** `bash scripts/andrea_doctor.sh --offline --receipt
  data/andrea-doctor-receipt.json`, then open
  `http://127.0.0.1:8765/dashboard`. The receipt option still refuses to run
  without `--offline`; the dashboard uses the same code-owned validator as the
  CLI and does not make a live probe.
- **Holds:** no live send, Private API off, no BlueBubbles live send, no
  credential writes, no merge/tag/deploy/gateway restart unless the owner asks.

## Separate follow-ups, not solved by this UI slice

- The backend's stale-receipt refresh packet drops prior failed-stage evidence
  and changes the suggested actor. Review that recovery policy separately; a
  refreshed dashboard is not proof that an underlying readiness blocker cleared.
- Failed-stage doctor recovery text can target `/tmp/andrea-doctor-receipt.json`
  while the dashboard consumes `data/andrea-doctor-receipt.json`.
- `andrea_doctor.sh --offline` still honors optional `OPENCLAW_ENFORCE=1` and
  `ANDREA_SYNC_DOCTOR=1` flags. Do not enable those for offline verification;
  this session explicitly disabled them and all live probes. This dashboard
  change neither executes the command nor changes doctor/runtime behavior.

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
