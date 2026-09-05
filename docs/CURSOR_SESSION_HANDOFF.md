# Cursor session handoff snapshot

Last updated: 2026-09-05 canonical doctor-receipt destination product pass.

## Current draft handoff

- **Base:** exact main `d9246d61ee8253b82dbba3ed4605baf1d89f9cb4`;
  stale-receipt current-authority #23 already landed, not redone.
- **Branch:** `cursor/canonical-doctor-receipt-e175`.
- **Product delta:** the offline doctor, consume/verify/summary, dashboard
  refresh command, and `cursor_handoff` discovery now share one destination:
  `data/andrea-doctor-receipt.json`. `bash scripts/andrea_doctor.sh --offline`
  writes that ignored file unless `--receipt PATH` overrides. Stale and
  failed-stage next actions name the same path, so following the packet
  refreshes the evidence dashboard/handoff actually read. `/tmp` is still
  never auto-read. Receipt bytes are not rewritten to change freshness or
  destination.
- **Reuse:** existing schema 2/fingerprint, `consume_receipt`, dashboard
  snapshot, and handoff backends. Dashboard still remaps leftover `/tmp/`
  guidance from older receipts.
- **Verification:** focused receipt/dashboard/offline-doctor tests plus the
  existing offline integration gate. Exact local/hosted results and draft tip
  are recorded on the PR and Bob-the-Bot coord #20, not implied by this note.
- **Holds:** `services/andrea_sync/server.py` remains exact base blob
  `8c5efa82c51534d93503b9cb655ba3eeefe2d39c`; exact send fence and Private API
  OFF unchanged. No live runtime/probe/message, skills installation, service
  restart, credentials/settings mutation, merge/tag/release/sign/deploy.
- **Next:** Karen reviews the exact draft; Jeff retains any real host/live
  readiness decision. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

## Previous current-authority slice — shipped in #23

- `--verify` / `--consume` / `--summary` withdraw current authority from a
  correctly signed receipt older than 24 hours. `cursor_handoff` consults
  `--receipt`, `ANDREA_DOCTOR_RECEIPT`, or discovered
  `data/andrea-doctor-receipt.json` before live submit.
- Limit remaining after #23 and addressed here: consume/doctor still taught
  `/tmp`, so following the packet could not unstick dashboard/handoff.

## Previous recovery slice — shipped in #22

- Readiness recovery preserves verified old owner holds and failed-stage
  evidence as historical; one stable selectable command refreshes the
  canonical `data/` receipt; explicit `--offline` overrides inherited live
  options.

## Previous monitor slice — shipped in #21

- Resilient overview polling, task identity guards, and keyboard selection.
  See git history and merged #21 for the exact contract.

## Holds that remain locked

- `OUTBOUND_CONFIRM_RE` is unchanged (`send it` / `send it now` / `send now`).
- `services/andrea_sync/server.py` blob `8c5efa82` stays identical to base.
- Private API stays off. No live send, credential writes, merge/tag/deploy, or
  gateway restart unless the owner asks.
