# Cursor session handoff snapshot

Last updated: 2026-09-06 openclaw CLI receipt-consult product pass.

## Current draft handoff

- **Base:** exact main `0bdecde20b46936a2a08ed284226bc5ab311abfb`;
  missing-receipt actor #25 already landed, not redone. Canonical `data/`
  destination #24 is also already landed, not redone.
- **Branch:** `cursor/openclaw-cli-receipt-consult-2b04`.
- **Product delta:** live `cursor_openclaw.py create-agent` now consults the
  same offline doctor receipt `cursor_handoff` already uses (`--receipt`,
  `ANDREA_DOCTOR_RECEIPT`, or discovered `data/andrea-doctor-receipt.json`).
  A consulted stale, invalid, missing-path, or not-autonomous receipt blocks
  the Cloud Agents POST. Diagnose and `--dry-run` report the consult and do
  not launch work. Absent (unconsulted) evidence is still not a new gate.
  `/tmp` is still never auto-read. `followup` is unchanged.
- **Reuse:** existing schema 2/fingerprint, `consult_receipt_for_handoff`,
  and `live_handoff_block_reason(..., "api")`. No receipt-byte rewrite.
- **Verification:** focused `cursor_openclaw` receipt-consult tests plus the
  existing offline integration gate. Exact local/hosted results and draft
  tip are recorded on the PR and Bob-the-Bot coord #20, not implied by this
  note.
- **Holds:** `services/andrea_sync/server.py` remains exact base blob
  `8c5efa82c51534d93503b9cb655ba3eeefe2d39c`; exact send fence and Private API
  OFF unchanged. No live runtime/probe/message, skills installation, service
  restart, credentials/settings mutation, merge/tag/release/sign/deploy.
- **Next:** Karen reviews the exact draft; Jeff retains any real host/live
  readiness decision. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

## Previous missing-receipt slice — shipped in #25

- `--consume` / `--verify` / `--summary` no longer treat a missing receipt as
  an owner hold. Invalid or tampered artifacts stay owner-blocked.

## Previous destination slice — shipped in #24

- Offline doctor, consume/verify/summary, dashboard refresh, and
  `cursor_handoff` discovery share `data/andrea-doctor-receipt.json`.

## Previous current-authority slice — shipped in #23

- `--verify` / `--consume` / `--summary` withdraw current authority from a
  correctly signed receipt older than 24 hours.

## Previous recovery slice — shipped in #22

- Readiness recovery preserves verified old owner holds and failed-stage
  evidence as historical.

## Previous monitor slice — shipped in #21

- Resilient overview polling, task identity guards, and keyboard selection.

## Holds that remain locked

- `OUTBOUND_CONFIRM_RE` is unchanged (`send it` / `send it now` / `send now`).
- `services/andrea_sync/server.py` blob `8c5efa82` stays identical to base.
- Private API stays off. No live send, credential writes, merge/tag/deploy, or
  gateway restart unless the owner asks.
