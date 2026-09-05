# Cursor session handoff snapshot

Last updated: 2026-09-05 doctor-receipt current-authority product pass.

## Current draft handoff

- **Base:** exact main `173789a7ae05d38408b4a90e0c2cdf762bc645b3`;
  offline recovery #22 already landed, not redone.
- **Branch:** `cursor/receipt-current-authority-9671`.
- **Product delta:** `--verify` / `--consume` / `--summary` now withdraw
  current authority from a correctly signed receipt whose local file is older
  than 24 hours. Last verified owner holds and failed stages stay historical.
  `cursor_handoff` consults that same packet before a live submit: explicit
  `--receipt`, `ANDREA_DOCTOR_RECEIPT`, or discovered
  `data/andrea-doctor-receipt.json`. `/tmp` is never auto-read. Missing
  evidence is not a new gate. Stale/invalid/not-autonomous receipts block
  Cursor API submit; receipts that disallow offline code also block local CLI
  submit. Diagnose and dry-run report the consult only.
- **Reuse:** existing schema 2/fingerprint, `consume_receipt`, dashboard
  historical-hold projection, and handoff backends. Receipt bytes are not
  rewritten to change freshness or destination.
- **Verification:** focused receipt/dashboard/handoff tests plus the existing
  offline integration gate. Exact local/hosted results and draft tip are
  recorded on the PR and Bob-the-Bot coord #20, not implied by this note.
- **Holds:** `services/andrea_sync/server.py` remains exact base blob
  `8c5efa82c51534d93503b9cb655ba3eeefe2d39c`; exact send fence and Private API
  OFF unchanged. No live runtime/probe/message, skills installation, service
  restart, credentials/settings mutation, merge/tag/release/sign/deploy.
- **Next:** Karen reviews the exact draft; Jeff retains any real host/live
  readiness decision. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

## Previous recovery slice — shipped in #22

- **Base:** exact `main` `10863501a421fb3d17e52e8b02f0423ccc2318ff`.
- **Product change:** readiness recovery preserves verified old owner holds
  and failed-stage evidence as historical; one stable selectable command
  refreshes the canonical `data/` receipt; explicit `--offline` overrides
  inherited live options.
- **Limit remaining after #22 and addressed here:** dashboard freshness was
  not applied to CLI consume/verify/summary or to `cursor_handoff` live submit.

## Previous monitor slice — shipped in #21

- Resilient overview polling, task identity guards, and keyboard selection.
  See git history and merged #21 for the exact contract.

## Holds that remain locked

- `OUTBOUND_CONFIRM_RE` is unchanged (`send it` / `send it now` / `send now`).
- `services/andrea_sync/server.py` blob `8c5efa82` stays identical to base.
- Private API stays off. No live send, credential writes, merge/tag/deploy, or
  gateway restart unless the owner asks.

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

## Optional triage for the next agent

Repo includes `scripts/handoff_context.py` (used by `cursor_handoff`); you can generate pre-handoff git triage from that tooling if needed.
