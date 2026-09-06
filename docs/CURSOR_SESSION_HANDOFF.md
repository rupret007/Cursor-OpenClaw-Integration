# Cursor session handoff snapshot

Last updated: 2026-09-06 missing-receipt actor product pass.

## Current draft handoff

- **Base:** exact main `e532b713442c66f100b7affeef87e394cfd027c1`;
  canonical `data/` doctor receipt #24 already landed, not redone.
- **Branch:** `cursor/missing-receipt-actor-1e30`.
- **Product delta:** `--consume` / `--verify` / `--summary` no longer treat a
  missing receipt as an owner hold. Missing evidence is still blocked for
  autonomy, but the coding agent may continue offline code and refresh the
  canonical `data/` receipt. Invalid or tampered artifacts stay owner-blocked.
  Dashboard missing snapshots now read that same consume packet. An explicit
  missing consult still blocks live Cursor API submit and no longer blocks
  local CLI submit. Absent (unconsulted) evidence is still not a new gate.
- **Reuse:** existing schema 2/fingerprint, `consume_receipt`, dashboard
  snapshot, and handoff backends. No receipt-byte rewrite.
- **Verification:** focused receipt/dashboard/offline-doctor/handoff tests plus
  the existing offline integration gate. Exact local/hosted results and draft
  tip are recorded on the PR and Bob-the-Bot coord #20, not implied by this
  note.
- **Holds:** `services/andrea_sync/server.py` remains exact base blob
  `8c5efa82c51534d93503b9cb655ba3eeefe2d39c`; exact send fence and Private API
  OFF unchanged. No live runtime/probe/message, skills installation, service
  restart, credentials/settings mutation, merge/tag/release/sign/deploy.
- **Next:** Karen reviews the exact draft; Jeff retains any real host/live
  readiness decision. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

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
