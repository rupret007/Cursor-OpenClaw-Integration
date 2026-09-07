# Cursor session handoff snapshot

Last updated: 2026-09-07 followup dry-run / CLI receipt honesty after #27.

## Current draft handoff

- **Base:** exact main `ea95e62f1912ac9e2841b0453f963ddea06f5ca0`;
  followup receipt/agent honesty #27 already landed, not redone. Canonical
  `data/` destination #24, missing-receipt actor #25, and create-agent
  consult #26 are also already landed, not redone.
- **Branch:** `cursor/followup-dry-run-honesty-31c6`.
- **Product delta:** followup CLI receipts no longer treat a clear doctor
  receipt as a clear followup. `--dry-run` still does not GET or POST.
  `receipt_would_block` stays the receipt gate. `followup_would_block` is
  that receipt reason when the receipt blocks, otherwise the code-owned
  `agent_not_checked` reason. `followup_ready` is always false on dry-run.
  Live success now includes the same allowlisted `doctor_receipt` consult
  (no path or fingerprint) plus `followup_ready=true`. `cursor_handoff --op
  followup` uses the same contract. `stop` / `delete` / `status` /
  `create-agent` write paths are unchanged.
- **Reuse:** existing schema 2/fingerprint, `consult_receipt_for_handoff`,
  `live_handoff_block_reason(..., "api")`, and shared
  `classify_followup_agent`. New shared `followup_dry_run_fields` in
  `cursor_api_common` (mirrored under the skill). No receipt-byte rewrite.
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

## Previous followup leftover — shipped in #27

- Live `cursor_openclaw.py followup` consults the same offline doctor
  receipt `create-agent` already uses, then refuses a missing or terminal
  agent before POSTing.

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
