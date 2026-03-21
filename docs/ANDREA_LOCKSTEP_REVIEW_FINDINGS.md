# Andrea lockstep — deep review findings (Phase 1)

## Capability / awareness drift

- **Root cause:** Channel UIs (Telegram bot, LLM replies) can assert “missing skill” from **stale memory** or **heuristics** without re-checking `openclaw skills list` / capability matrix truth.
- **Gap:** `andrea_sync` ingested messages and cursor lifecycle but had **no canonical capability snapshot** in the event store or HTTP API for other components to consult.
- **Mitigation implemented:** `PublishCapabilitySnapshot` command + `GET /v1/capabilities` + `policy.evaluate_skill_absence_claim()` (verify-before-deny TTL).

## Kill switch / safety

- **Gap:** No global halt for ingress; `/v1/commands` had no admin auth boundary for destructive/control operations.
- **Mitigation implemented:** `KillSwitchEngage` / `KillSwitchRelease` (internal-auth only on `/v1/commands`), env/file/meta tri-state `is_kill_switch_engaged()`, HTTP **503** on Telegram/Alexa/command ingress when engaged (release still allowed with token).

## Webhook security

- **Gap:** Reliance on query `?secret=` only (visible in logs/proxies).
- **Mitigation implemented:** Optional `X-Telegram-Bot-Api-Secret-Token` verification (`ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET`) per Telegram Bot API guidance; query secret remains supported for migration.

## Follow-ups (not blocking)

- Named Cloudflare tunnel + stable hostname for production (quick tunnels still OK for POC).
- Automatic Telegram replies from projected task state (notifier worker).
- Optional `ANDREA_SYNC_COMMAND_TOKEN` to protect all `/v1/commands` if exposed beyond localhost.
