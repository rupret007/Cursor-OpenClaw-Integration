# Andrea comms (Telegram) + productivity lane

Operational patterns for **inbound tasks**, **status**, **escalation**, and **personal productivity** routines. Adjust IDs/tokens via `.env` (never commit).

---

## 1. Telegram operational flows

### 1.1 Inbound task capture

- **Normalize** each inbound message into: goal, deadline (if any), constraints, links.
- **Ack** quickly with a one-line receipt (“captured: …”) when the channel is operational.
- **Track** work items in the user’s system of record (issues, notes file, or task list) — don’t rely only on chat scrollback.

### 1.2 Status / heartbeat

- After non-trivial work: short summary (done / blocked / next).
- If a step will take >~15 minutes: send a “still running: …” ping with ETA.
- On failure: error class + next action + whether human input is needed.

### 1.3 Escalation

Escalate to the human when:

- Confirm-required per [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md)
- Auth drift (`gh auth`, API 401, Telegram 401)
- Repeated tool failure after one fallback attempt
- Ambiguous instructions that change cost/risk (money, data loss, external commitments)

**Retry policy:** transient errors → backoff (e.g. 2s, 5s, 15s); don’t tight-loop APIs.

### 1.4 Channel hygiene

- Never paste raw API keys into Telegram.
- Prefer references (“key rotated”, “token updated in .env”) over secret material.

---

## 2. Productivity routines

### 2.1 Daily

- **Morning:** capability snapshot (`python3 scripts/andrea_capabilities.py`) on active dev days.
- **Plan:** top 3 outcomes; blockers explicit.
- **Shutdown:** handoff note — what landed, what’s queued, commands to resume.

### 2.2 Weekly

- Review open PRs/issues; prune stale branches.
- Refresh docs if workflows changed (especially `ANDREA_*` and `DEPLOYMENT.md`).

### 2.3 Patterns

- **Handoff summary template**

```text
Context: …
Done: …
Next: …
Blockers: …
Commands: …
```

- **Reminder notes** — store in repo `docs/` or external notes; link from chat instead of duplicating long specs.

---

## 3. Environment (boolean reference)

See [.env.example](../.env.example): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Presence is reported by `scripts/andrea_capabilities.py` without exposing values.

---

## 4. Related docs

- [ANDREA_CAPABILITY_MATRIX.md](ANDREA_CAPABILITY_MATRIX.md)
- [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md)
- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md)
