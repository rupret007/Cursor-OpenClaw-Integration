# Cursor session handoff snapshot

Last updated: 2026-09-06 honest followup leftover after #26.

## Current draft handoff

- **Base:** exact main `55c6d8eff5a2af5fb3c8257e35ce576be1d0afbb`;
  create-agent receipt consult #26 already landed, not redone. Canonical
  `data/` destination #24 and missing-receipt actor #25 are also already
  landed, not redone.
- **Branch:** `cursor/followup-receipt-agent-honesty-2b71`.
- **Product delta:** live `cursor_openclaw.py followup` now consults the
  same offline doctor receipt `create-agent` already uses (`--receipt`,
  `ANDREA_DOCTOR_RECEIPT`, or discovered `data/andrea-doctor-receipt.json`).
  A consulted stale, invalid, missing-path, or not-autonomous receipt blocks
  the followup POST. After that consult, followup `GET`s agent status and
  refuses a missing, unknown, or terminal agent instead of POSTing more
  work. Diagnose is unchanged. `--dry-run` reports the consult plus
  `agent_state=not_checked` and does not GET or POST. Absent (unconsulted)
  evidence is still not a new gate. `/tmp` is still never auto-read.
  `cursor_handoff --op followup` already consulted the receipt; it now uses
  the same allowlisted agent-state check. `stop` / `delete` / `status` are
  unchanged.
- **Reuse:** existing schema 2/fingerprint, `consult_receipt_for_handoff`,
  `live_handoff_block_reason(..., "api")`, and shared
  `classify_followup_agent`. No receipt-byte rewrite.
- **Verification:** focused `cursor_openclaw` / `cursor_api_common` /
  `cursor_handoff` followup tests plus the existing offline integration
  gate. Exact local/hosted results and draft tip are recorded on the PR
  and Bob-the-Bot coord #20, not implied by this note.
- **Holds:** `services/andrea_sync/server.py` remains exact base blob
  `8c5efa82c51534d93503b9cb655ba3eeefe2d39c`; exact send fence and Private API
  OFF unchanged. No live runtime/probe/message, skills installation, service
  restart, credentials/settings mutation, merge/tag/release/sign/deploy.
- **Next:** Karen reviews the exact draft; Jeff retains any real host/live
  readiness decision. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

## Previous create-agent consult slice — shipped in #26

- Live `cursor_openclaw.py create-agent` consults the same offline doctor
  receipt `cursor_handoff` already uses.

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
