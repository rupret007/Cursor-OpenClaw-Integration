# Andrea lockstep architecture

Strict two-way sync between **user channels** (Telegram, Alexa, CLI), **OpenClaw**, and **Cursor** uses a **single local command/event store**. No channel is authoritative alone; projected **task state** is derived from append-only events.

## Components

| Piece | Path | Role |
|-------|------|------|
| Schema | `services/andrea_sync/schema.py` | Command types, event types, task status, idempotency rules |
| Store | `services/andrea_sync/store.py` | SQLite WAL (`data/andrea_sync.db` by default) |
| Bus | `services/andrea_sync/bus.py` | Accept commands, enforce idempotency, append events |
| Projector | `services/andrea_sync/projector.py` | Derive task JSON from events |
| HTTP API | `services/andrea_sync/server.py` | REST ingress for commands, Telegram webhook, Alexa skill |
| Optimizer | `services/andrea_sync/optimizer.py` + `scripts/andrea_optimize.py` | Detect regressions, emit proposals, and gate local self-heal |
| Experience assurance | `services/andrea_sync/experience_assurance.py` + `scripts/andrea_experience_cycle.py` | Replay deterministic Andrea scenarios, score UX/routing/capability honesty, persist runs, and optionally bridge failures into repair |
| Incident repair | `services/andrea_sync/repair_orchestrator.py` + `scripts/andrea_repair_cycle.py` | Detect concrete failures, triage them, try the smallest safe repair, verify, rollback, and escalate to Cursor when needed |
| Dashboard | `services/andrea_sync/dashboard.py` | Operator summary for orchestration, memory, reminders, and autonomy health |
| Policy | `services/andrea_sync/policy.py` | Verify-before-deny using published capability digest + TTL |
| Kill switch | `services/andrea_sync/kill_switch.py` | Env + flag file + meta; halts ingress when engaged |
| Server entry | `scripts/andrea_sync_server.py` | Run from repo root |
| Cursor CLI hook | `scripts/andrea_sync_cursor_report.py` | Emit lifecycle events (HTTP or `--db`) |
| Health | `scripts/andrea_sync_health.py` | Optional doctor probe |

## HTTP API (v1)

| Method | Path | Notes |
|--------|------|--------|
| GET | `/v1/health` | Liveness + db path + `kill_switch` summary + capability digest age |
| GET | `/v1/status` | Extended JSON: kill switch + full capability digest payload |
| GET | `/v1/capabilities` | Cached capability snapshot (from last `PublishCapabilitySnapshot`) |
| GET | `/v1/dashboard/summary` | Operator JSON summary for service health, optimization, experience assurance, and projected task state |
| GET | `/v1/policy/skill-absence?skill=...` | Verify-before-deny: may a channel claim this skill is absent? (`max_age_seconds` optional) |
| POST | `/v1/commands` | JSON command envelope (see schema). Admin commands require `Authorization: Bearer $ANDREA_SYNC_INTERNAL_TOKEN` |
| GET | `/v1/tasks` | Recent tasks (`?limit=`) |
| GET | `/v1/tasks/{id}` | Projected state + event list |
| POST | `/v1/internal/events` | Append raw event; requires `Authorization: Bearer $ANDREA_SYNC_INTERNAL_TOKEN` |
| POST | `/v1/telegram/webhook?secret=...` | Telegram `Update` JSON; optional `X-Telegram-Bot-Api-Secret-Token` when `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` is set |
| POST | `/v1/alexa` | Alexa skill request JSON; returns a short voice-safe response, persists task state, and optionally requires a forwarded edge token |

**Admin command types** (must use `"channel":"internal"` + Bearer token on `/v1/commands`):

- `PublishCapabilitySnapshot` — body is typically `scripts/andrea_capabilities.py --json` output; sets canonical digest for policy.
- `KillSwitchEngage` / `KillSwitchRelease` — emergency halt / resume (see `scripts/andrea_kill_switch.sh`).
- `RunOptimizationCycle` / `CreateOptimizationProposal` / `ApplyOptimizationProposal` — autonomy loop entrypoints for recurring UX/runtime failures.
- `RunIncidentRepair` — incident-driven repair loop entrypoint: verification-backed detection, multi-model triage/patch planning, isolated attempts, rollback, and optional Cursor escalation.
- `SavePrincipalMemory` / `SetPrincipalPreference` / `LinkPrincipalIdentity` — durable identity and memory controls.
- `CreateReminder` / `RunProactiveSweep` — quiet follow-through and reminder delivery primitives.

When the kill switch is engaged, Telegram/Alexa ingress and normal commands return **503**; only `KillSwitchRelease` is accepted (with token).

## Environment

| Variable | Purpose |
|----------|---------|
| `ANDREA_SYNC_DB` | Override SQLite path (default: `<repo>/data/andrea_sync.db`) |
| `ANDREA_SYNC_PORT` | Listen port (default `8765`) |
| `ANDREA_SYNC_TELEGRAM_SECRET` | Query `secret` for Telegram webhook (optional if header secret used) |
| `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` | Telegram `secret_token`; verified via `X-Telegram-Bot-Api-Secret-Token` |
| `ANDREA_SYNC_INTERNAL_TOKEN` | Bearer token for admin `/v1/commands` and `/v1/internal/events` |
| `ANDREA_SYNC_KILL_SWITCH` | If `1`/`true`, forces kill switch engaged (process env) |
| `ANDREA_SYNC_KILL_FILE` | Override path for kill flag file (default: `<db-file>.kill` beside active DB) |
| `ANDREA_SYNC_URL` | Base URL for health probe / `cursor_report` HTTP mode |
| `ANDREA_SYNC_DOCTOR` | Set `1` to run health during `andrea_doctor.sh` |
| `ANDREA_SYNC_REQUIRED` | If `1`, health probe fails when URL unreachable |
| `ANDREA_SYNC_VERBOSE` | `1` logs HTTP requests to stderr |
| `TELEGRAM_BOT_TOKEN` | Needed for Telegram ACK/progress/final replies |
| `CURSOR_API_KEY` | Needed for default Telegram -> Cursor executor flow |
| `ANDREA_CURSOR_REPO` | Optional repo path for Telegram-triggered Cursor handoff (default repo root) |
| `ANDREA_CURSOR_HANDOFF_MODE` | `auto` / `api` / `cli` for the built-in Telegram executor |
| `ANDREA_SYNC_PUBLIC_BASE` | Public HTTPS origin for Telegram webhook self-heal |
| `ANDREA_SYNC_ALEXA_EDGE_TOKEN` | Optional shared secret expected from the Alexa cloud edge (`Authorization: Bearer ...` or `X-Andrea-Alexa-Edge-Token`) |
| `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM` | If `1`, send one Telegram summary for each completed/failed Alexa task |
| `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID` | Telegram chat id for Alexa session summaries (falls back to `TELEGRAM_CHAT_ID`) |
| `ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED` | If `1`, the server runs a background reminder sweep loop |
| `ANDREA_SYNC_PROACTIVE_SWEEP_INTERVAL_SECONDS` | How often the reminder sweep checks for due reminders (default `60`) |
| `ANDREA_SYNC_CURSOR_REPO` | Repo override for admin/autonomy helpers such as local self-heal |
| `ANDREA_SELF_HEAL_CURSOR_MODE` | Cursor backend override for auto-heal branch prep (`auto`, `api`, `cli`) |
| `ANDREA_SYNC_BACKGROUND_INCIDENT_REPAIR_ENABLED` | If `1`, the idle background optimizer also runs the incident repair loop |
| `ANDREA_SYNC_BACKGROUND_INCIDENT_CURSOR_EXECUTE` | If `1`, deep repair plans created by the background repair loop may auto-escalate into Cursor |
| `ANDREA_REPAIR_ENABLED` | Global enable/disable switch for the incident repair control plane |
| `ANDREA_REPAIR_PROMPT_VERSION` + per-role `ANDREA_REPAIR_*_PROMPT_VERSION` | Prompt contract version pins for triage, patching, planning, and handoff |
| `ANDREA_REPAIR_CURSOR_MODE` | Cursor backend override for deep repair escalation (`auto`, `api`, `cli`) |
| `ANDREA_REPAIR_SAFE_ROOTS` | Colon/comma-separated override for repo-safe auto-repair roots |
| `ANDREA_REPAIR_MAX_PATCH_ATTEMPTS` | Lightweight patch attempts before deep escalation (default `2`) |
| `ANDREA_REPAIR_MAX_MODEL_INVOCATIONS` / `ANDREA_REPAIR_MAX_CHANGED_LINES` | Per-incident budget caps for model calls and patch scope |
| `ANDREA_REPAIR_STRICT_MODEL_MATCH` | If `1`, fail a repair lane when reported provider/model does not match the requested route |

## Idempotency

Commands without `idempotency_key` use a deterministic hash of `channel`, `external_id`, and `command_type`. Duplicate deliveries append `CommandDeduped` and return the same `task_id`.

## Cursor / OpenClaw wiring

1. A normal Telegram message lands on `/v1/telegram/webhook`, becomes `SubmitUserMessage`, and is persisted into lockstep first.
2. `services/andrea_sync/server.py` applies Andrea-first routing:
   - direct Andrea reply for lightweight conversational/personal assistant turns
   - OpenClaw hybrid delegation for productivity / assistant-skill requests and, by default, heavier repo or coding work
   - explicit Telegram intent hints can override heuristics:
     - `@Andrea ...` keeps the turn in Andrea's direct assistant lane unless the user only asked for routing help
     - `@Cursor ...` makes the turn Cursor-first, but still through Andrea/OpenClaw coordination so the shared timeline stays intact
     - `@Andrea @Cursor ...` or phrases like `work together` / `double-check` trigger collaborative mode, where OpenClaw is expected to involve Cursor before the final answer
   - direct Andrea replies can also look at recent principal history, stored notes, and preferences before answering
3. Delegated tasks are queued as `JobQueued` with an execution lane:
   - `openclaw_hybrid` starts `scripts/andrea_sync_openclaw_hybrid.py`, which runs `openclaw agent` against the main OpenClaw runtime and asks it to use hybrid skills first or escalate via `cursor_handoff` when the request becomes repo-heavy
   - `direct_cursor` remains available as a fallback lane when you explicitly force Cursor-first behavior
4. Delegated lifecycle is appended back into lockstep as `JobStarted`, `JobProgress`, `JobCompleted`, or `JobFailed`, with metadata showing whether OpenClaw stayed in-lane or escalated to Cursor, plus the user's routing hint / collaboration mode when present.
5. Multi-model collaboration is logged explicitly through `OrchestrationStep` events so plan, critique, execution, and synthesis are auditable without turning raw tool chatter into user-facing copy.
6. The same server process posts Telegram replies from projected task state, not ad-hoc chat text.
7. Telegram replies are formatted as:
   - `Andrea:` user-facing answer first
   - summary mode stays calm and compact
   - full-dialogue mode shows a curated collaboration trace (plan / critique / execution / synthesis), not runtime/session jargon
   - exact diagnostics stay in internal traces, task metadata, dashboard views, and optimizer findings
8. Direct Andrea replies intentionally skip Cursor lifecycle noise.
9. When `ANDREA_SYNC_PUBLIC_BASE` is configured, the server also self-heals Telegram webhook registration if another process clears it.

## Alexa voice lane

1. A spoken `Ask AndreaBot ...` turn lands on `/v1/alexa`.
2. The server stores an `AlexaUtterance` task with Alexa session metadata in `meta.alexa`.
3. Andrea-first routing runs immediately:
   - direct conversational turns return a spoken reply synchronously
   - heavier work becomes `JobQueued` and continues through the existing OpenClaw/Cursor lanes
4. Alexa does not receive lifecycle spam; instead, the backend can send one Telegram summary when the task reaches `completed` or `failed`.
5. In the recommended production shape, Alexa signature validation happens at the public cloud edge, which forwards the raw request body plus `ANDREA_SYNC_ALEXA_EDGE_TOKEN` to the private/local Andrea server.

## Principal memory and proactive surface

1. Principals are durable identities linked across Telegram chats, Alexa users, and future channels.
2. Each principal can accumulate memory notes, preferences, and reminders without exposing that internal storage model to the user.
3. Simple assistant actions like “remember this” or “remind me tomorrow” can complete directly inside Andrea without invoking a heavy collaboration lane.
4. Reminder delivery can happen either from the server’s background sweep (`ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED=1`) or on demand through the `RunProactiveSweep` admin command.

## Closed-loop local self-heal

1. `services/andrea_sync/optimizer.py` scans recent outcomes, derives recurring UX/runtime failure categories, and emits structured optimization proposals.
2. `scripts/andrea_optimize.py` runs one optimization cycle, optionally records regression results, and can auto-apply ready proposals through Cursor branch prep.
3. `services/andrea_sync/experience_assurance.py` replays deterministic scenarios against a temporary lockstep server, emits a `verification_report`-compatible payload, persists the latest run/checks, and can forward failures into the same incident repair lane without inventing a parallel repair system.
4. `services/andrea_sync/repair_orchestrator.py` adds a first-class incident pipeline: detect from failing verification, triage with the Gemini lane, try a small GPT patch, challenge it with MiniMax if needed, then create a deep GPT repair plan and optional Cursor handoff only after the lightweight paths fail.
5. `scripts/andrea_repair_cycle.py` runs that pipeline directly, while `RunIncidentRepair` exposes the same flow on the internal admin command surface.
6. `scripts/andrea_autonomy_cycle.sh` is the operator-facing wrapper for a disciplined local autonomy pass: health check, regressions, optimization, incident-driven repair, gated auto-heal, and proactive sweep.
7. Auto-heal, experience replay, and repair are intentionally gated by regression success, kill-switch state, capability freshness, safe file roots, isolated worktrees, verification, and rollback so the system improves itself without silently rewriting arbitrary parts of the repo.
8. Runtime skill truth is shared across messaging, Apple Notes, and Apple Reminders: Andrea verifies the current capability digest, attempts the smallest safe heal when a lane is not verified, and keeps user-facing copy calm instead of exposing raw OpenClaw/runtime diagnostics.

## Security

- Do not expose `/v1/internal/events` without a strong random `ANDREA_SYNC_INTERNAL_TOKEN`.
- Prefer Telegram `setWebhook` **`secret_token`** + `X-Telegram-Bot-Api-Secret-Token` verification; query `secret=` remains supported as a fallback.
- Admin commands on `/v1/commands` require the same Bearer token; do not expose that endpoint publicly without TLS + network ACLs.
- For Alexa, prefer a small public cloud edge that performs Alexa signature verification and forwards to `/v1/alexa` with `ANDREA_SYNC_ALEXA_EDGE_TOKEN`.
- Treat the SQLite file like a journal: backup with the rest of your operator secrets.

## Review notes

Design/gap analysis: [ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](ANDREA_LOCKSTEP_REVIEW_FINDINGS.md).

## macOS auto-start

Templates + installer: `scripts/macos/install_andrea_launchagents.sh` (optional named `cloudflared`, optional `localtunnel` fallback, optional OpenClaw login refresh, plus a post-login bootstrap step for capability publish + webhook ensure). The sync LaunchAgent sources repo `.env` first, then `~/andrea-lockstep.env` for per-machine overrides.

This keeps the same assistant persona available across text-first Telegram now and voice-first Alexa later: Andrea answers first, then delegates when the work needs a heavier technical lane.

## Operator full cycle

`bash scripts/andrea_full_cycle.sh` — git pull, `/v1/health` + `/v1/status`, capability publish, policy probe, optional `openclaw gateway restart`, communication smoke, kill-switch drill, optional Telegram `webhook-info`. Requires `ANDREA_SYNC_INTERNAL_TOKEN` and a running `andrea_sync` server at `ANDREA_SYNC_URL`.

## Operations

Telegram ingest over the public internet: [ANDREA_TELEGRAM_LOCKSTEP_E2E.md](ANDREA_TELEGRAM_LOCKSTEP_E2E.md).

```bash
# Start server (repo root)
python3 scripts/andrea_sync_server.py

# Manual command
curl -sS -X POST http://127.0.0.1:8765/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command_type":"CreateTask","channel":"cli","payload":{"summary":"demo"}}'
```

See [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md) for voice.
