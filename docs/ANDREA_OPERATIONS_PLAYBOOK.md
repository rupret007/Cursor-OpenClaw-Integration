# Andrea operations playbook

Single handoff doc for **who Andrea is**, **what she can do autonomously**, **what needs human approval**, and **how to recover** from common failures.

---

## 1. Role

**Andrea** is a high-autonomy personal operator built on **OpenClaw** + **Cursor-OpenClaw-Integration**: coding/DevOps, Telegram/comms patterns, and productivity routines — with **execute-first** behavior and **safety guardrails** for destructive work.

---

## 2. Autonomous (default — no extra ask)

- Read/search code and docs; propose patches; run **local** tests and linters.
- Use **dry-run** and **diagnose** paths for Cursor / handoff CLIs.
- Git workflow on a **feature branch**: commit, push, open PR when the user’s norm allows.
- Non-destructive `gh` queries; capability / reliability probes documented here.
- Telegram **ack / status** patterns in [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md).

Full policy: [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md).

---

## 3. Human approval required

- Irreversible deletion, production data changes, security weakening, billing, org-wide Git settings.
- Force-push to shared branches; merging with broken CI (unless user explicitly overrides).
- External commitments or legal-sensitive messaging.

See **confirm-required** table in [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md).

---

## 4. Startup self-check (before big sessions)

**One command (recommended):** security sanity + capability snapshot + readiness grade + reliability probes + optional OpenClaw model probe:

```bash
cd /path/to/Cursor-OpenClaw-Integration
bash scripts/andrea_doctor.sh
# CI / headless: skip live OpenClaw probe
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
# Treat security warnings as failures:
# STRICT_SECURITY=1 bash scripts/andrea_doctor.sh
# Auto-remediate failed model probe using profile guard:
# MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
# Enforce OpenClaw baseline first (skill sync + required skills + probe):
# OPENCLAW_ENFORCE=1 bash scripts/andrea_doctor.sh
```

Manual steps (same ingredients):

```bash
python3 scripts/andrea_capabilities.py
python3 scripts/andrea_readiness_grade.py   # A/B/C; exits 1 on C
bash scripts/andrea_security_sanity.sh
```

Optional strict gate (fails if critical capabilities blocked):

```bash
python3 scripts/andrea_capabilities.py --strict
```

**Reboot-ready login path (macOS):**

```bash
cd /path/to/Cursor-OpenClaw-Integration
export CLOUDFLARED_TUNNEL_TOKEN='...'
bash scripts/andrea_services.sh install-launchagents --with-cloudflared --load
```

Fallback for hosts without `cloudflared`:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_LOCALTUNNEL_SUBDOMAIN='fine-monkeys-shake'
bash scripts/andrea_services.sh install-launchagents --with-localtunnel --load
```

This loads three key login jobs:

- `com.andrea.andrea-sync` to keep the local lockstep server alive
- `com.andrea.andrea-cloudflared` for a stable named tunnel
- `com.andrea.andrea-post-login-bootstrap` to sync the OpenClaw skill mirror, restart the gateway, publish capabilities, and ensure the Telegram webhook

Admin control after install:

```bash
bash scripts/andrea_services.sh status all
bash scripts/andrea_services.sh restart all
bash scripts/andrea_services.sh bootstrap
```

Put persistent overrides in `~/andrea-lockstep.env` when you want them to survive reboot without modifying the repo `.env`.

The old `--with-openclaw-refresh` LaunchAgent remains available only as a compatibility shim. The bootstrap path already restarts the gateway, and duplicate login-time restarts are debounced automatically when both are present.

SLO-style gate (grade + optional `openclaw models status --probe`; **probe timeout is ms**):

```bash
bash scripts/andrea_slo_check.sh
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_slo_check.sh
```

---

## 5. Verification stack

| Step | Command |
|------|---------|
| **Daily wrap-up (operator)** | Step-by-step: [ANDREA_WRAP_UP_DAILY.md](ANDREA_WRAP_UP_DAILY.md) — `bash scripts/andrea_wrap_up_prereqs.sh` → `bash scripts/andrea_full_cycle.sh` → `bash scripts/test_integration.sh` |
| Unit + integration | `bash scripts/test_integration.sh` |
| Live comm smoke (optional) | `RUN_COMM_SMOKE=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/test_integration.sh` or `bash scripts/andrea_communication_smoke.sh` |
| Local monitor dashboard | Open `http://127.0.0.1:8765/dashboard` for live health, webhook, recent tasks, and task timelines |
| Admin service control | `bash scripts/andrea_services.sh status all` for the current runtime, `bash scripts/andrea_services.sh restart all` to bounce sync+tunnel+gateway, `bash scripts/andrea_services.sh bootstrap` to rerun the login heal chain on demand |
| Full operator cycle (local) | From repo: `export ANDREA_SYNC_INTERNAL_TOKEN=…` then `bash scripts/andrea_full_cycle.sh` (pull, health, publish digest, policy, gateway restart, smoke, kill-switch drill). Skips: `SKIP_GIT=1`, `SKIP_GATEWAY_RESTART=1`, `SKIP_COMM_SMOKE=1`, `SKIP_KILL_DRILL=1`, `SKIP_TELEGRAM_E2E=1`. |
| Masterclass doctor | `bash scripts/andrea_doctor.sh` |
| Closed-loop autonomy pass | `export ANDREA_SYNC_URL=… ANDREA_SYNC_INTERNAL_TOKEN=… && bash scripts/andrea_autonomy_cycle.sh` |
| Security sanity (repo) | `bash scripts/andrea_security_sanity.sh` |
| Readiness grade (A/B/C) | `python3 scripts/andrea_readiness_grade.py` |
| SLO check (grade + probe) | `bash scripts/andrea_slo_check.sh` |
| OpenClaw baseline enforce | `bash scripts/andrea_openclaw_enforce.sh` |
| Model remediation (auto profile failover) | `bash scripts/andrea_model_guard.sh` |
| Telegram getMe SLO (optional) | `TELEGRAM_SLO=1 bash scripts/andrea_slo_check.sh` (needs `TELEGRAM_BOT_TOKEN`) |
| Release gate (strict) | `bash scripts/andrea_release_gate.sh` |
| Reliability probes | `bash scripts/andrea_reliability_probes.sh` |
| Live API (optional) | `RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh` |
| Live host tools (optional) | `RUN_LIVE_PROBES=1 bash scripts/andrea_reliability_probes.sh` |

---

## 6. Recovery cheat sheet

| Symptom | Fix |
|---------|-----|
| `CURSOR_API_KEY missing` | `export CURSOR_API_KEY=…` or `bash scripts/setup_admin.sh` |
| `401` from Cursor API | Rotate key; check `CURSOR_BASE_URL` / `CURSOR_AUTH_MODE` |
| `gh` not logged in | `gh auth login` or set `GH_TOKEN` / `GITHUB_TOKEN` in `.env` (merge without full wizard: `python3 scripts/dotenv_set_key.py GH_TOKEN --skill`) |
| `openclaw` / skill missing | Install OpenClaw; `cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/`; `openclaw gateway restart`; `openclaw skills info cursor_handoff --json` |
| `acpx` missing even though `acp-router` is loaded | Install the ACP client with `npm install -g acpx`, restart the gateway, then rerun `bash scripts/andrea_openclaw_enforce.sh` or `python3 scripts/andrea_capabilities.py` |
| Reboot came back but Telegram is dark | Run `bash scripts/andrea_services.sh status all`, confirm the named tunnel LaunchAgent is loaded, and rerun `bash scripts/andrea_services.sh bootstrap` before checking `python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info --require-match` |
| SSL errors in Python | See README: `SSL_CERT_FILE` + `certifi` |
| Tests fail | Fix on a branch; do not merge to `main` until green |
| Readiness **Grade C** | `python3 scripts/andrea_capabilities.py` — unblock **blocked** rows (often `github:auth`: `gh auth login` or `python3 scripts/dotenv_set_key.py GH_TOKEN --skill`) |
| Pre-release strict gate | `bash scripts/andrea_release_gate.sh` |
| Lockstep server down | First try `bash scripts/andrea_services.sh restart all`; if you are intentionally outside LaunchAgents, start `python3 scripts/andrea_sync_server.py` and check `ANDREA_SYNC_URL` + `python3 scripts/andrea_sync_health.py` |
| Lockstep kill switch engaged | `GET /v1/status` shows `kill_switch.engaged`; run `bash scripts/andrea_kill_switch.sh release` (needs `ANDREA_SYNC_INTERNAL_TOKEN`) or clear env/file per [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md) |
| Capability / “missing skill” drift | Publish snapshot: `python3 scripts/andrea_sync_publish_capabilities.py`; channels should call `GET /v1/policy/skill-absence?skill=…` before denying a skill |
| Telegram webhook 403 | Match `?secret=` to `ANDREA_SYNC_TELEGRAM_SECRET` and/or header to `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET`; re-run `setWebhook` |
| Telegram `webhook-info` shows `"url": ""` | Telegram currently has no webhook registered. Re-check `ANDREA_SYNC_PUBLIC_BASE`, run `python3 scripts/andrea_lockstep_telegram_e2e.py set-webhook`, then verify with `python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info --require-match` |
| Telegram ingests but no reply | Confirm `TELEGRAM_BOT_TOKEN` is loaded by `python3 scripts/andrea_sync_server.py`; inspect `/v1/tasks/{id}` for `meta.telegram.chat_id` plus `meta.execution` / `meta.openclaw` / `meta.cursor` |
| Telegram task queues then fails immediately | Confirm `openclaw agent --agent main --message "READY" --json` succeeds, `openclaw skills info cursor_handoff --json` is eligible, and `CURSOR_API_KEY` + repo `origin` are available for OpenClaw escalations; inspect `/v1/tasks/{id}` `last_error` |
| `@Cursor` did not seem to involve Cursor | Inspect `/v1/tasks/{id}` `meta.execution.collaboration_mode` and `meta.execution.delegated_to_cursor`; if needed, rerun after confirming `cursor_handoff` is eligible and `CURSOR_API_KEY` is available |
| `@Gemini` / `@Minimax` / `@OpenAI` did not seem to honor the requested lane | Inspect `/v1/tasks/{id}` `meta.telegram.preferred_model_family`, `meta.execution.preferred_model_label`, and `meta.openclaw.provider` / `meta.openclaw.model`; the preferred lane is a strong routing instruction, but the final active provider/model may differ if OpenClaw falls back for reliability |
| Reminder created but never delivered | Confirm `ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED=1` or run `RunProactiveSweep` manually; check `dashboard` memory metrics and recent `ReminderFailed` / `ReminderDelivered` events |
| Auto-heal proposal refused to run locally | Check regression status, kill-switch state, capability digest freshness, safe target paths, and whether the repo was dirty when `bash scripts/andrea_autonomy_cycle.sh` ran |

---

## 7. Doc map

| Document | Purpose |
|----------|---------|
| [ANDREA_WRAP_UP_DAILY.md](ANDREA_WRAP_UP_DAILY.md) | End-of-day operator sequence (prereqs, full cycle, offline gate) |
| [ANDREA_SECURITY.md](ANDREA_SECURITY.md) | Secrets, redaction, gateway token, rotation |
| [ANDREA_MODEL_POLICY.md](ANDREA_MODEL_POLICY.md) | fast/balanced/deep profiles + rate-limit playbook |
| [ANDREA_CAPABILITY_MATRIX.md](ANDREA_CAPABILITY_MATRIX.md) | Live readiness matrix |
| [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md) | Execute-first + boundaries |
| [ANDREA_DEVOPS_RUNBOOK.md](ANDREA_DEVOPS_RUNBOOK.md) | Branch/PR + GitHub + fallbacks |
| [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md) | Telegram + routines + memory policy |
| [ANDREA_READINESS_REPORT.md](ANDREA_READINESS_REPORT.md) | Final readiness template / last run |
| [ANDREA_OPENCLAW_HYBRID_SKILLS.md](ANDREA_OPENCLAW_HYBRID_SKILLS.md) | Hybrid expansion (Apple/Google/Waves 1–3) |
| [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md) | Telegram/Alexa/Cursor shared command bus + SQLite event store |
| [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md) | Alexa Custom Skill endpoint + HTTPS notes |
| [ANDREA_ALEXA_USER_SETUP.md](ANDREA_ALEXA_USER_SETUP.md) | Step-by-step human setup guide for the Alexa app + Developer Console |
| [ANDREA_TELEGRAM_LOCKSTEP_E2E.md](ANDREA_TELEGRAM_LOCKSTEP_E2E.md) | Telegram webhook + cloudflared + lockstep verification |
| [ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](ANDREA_LOCKSTEP_REVIEW_FINDINGS.md) | Lockstep security/awareness review notes |
| [docs/DEPLOYMENT.md](DEPLOYMENT.md) | Branch + deployment baseline |

---

## 8. Maintenance

When adding a new secret key to `.env.example`, update `SECRET_KEYS` in `scripts/andrea_capabilities.py` so the matrix stays accurate.

Run `bash scripts/andrea_security_sanity.sh` before merging changes that touch env or provider wiring; use `STRICT=1` locally if you want backup-file warnings to fail the check.

---

## 9. Hybrid daily workflow (Apple + Google + meta lane)

Use this after Wave 1 skills are installed/auth’d (see **[ANDREA_OPENCLAW_HYBRID_SKILLS.md](ANDREA_OPENCLAW_HYBRID_SKILLS.md)**).

**Typical loop**

1. **Morning snapshot** — `python3 scripts/andrea_capabilities.py --json` (hybrid rows are `ready` or `ready_with_limits`; core OpenClaw skills must be `✓ ready` in `openclaw skills list`).
2. **Capture** — Apple Notes / Reminders / Things via their OpenClaw skills once `memo` / `remindctl` / `things` are on `PATH`.
3. **Google** — Mail/Calendar/Drive via `gog` after CLI install + OAuth per skill metadata.
4. **Compress** — URLs/transcripts via `summarize`; trawl prior sessions via `session-logs` (needs `jq` + `rg`).
5. **Gate** — `bash scripts/andrea_release_gate.sh` before merging infra changes; for live OpenClaw hygiene: `OPENCLAW_ENFORCE=1 MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh`.

**Strict eligibility (optional)** — When hybrid CLIs are meant to be mandatory on a machine, set `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` to a CSV of skill keys and run `bash scripts/andrea_openclaw_enforce.sh` (requires `jq`). Example keys: `bluebubbles`, `apple-notes`, `apple-reminders`, `gog`, `session-logs`.

**Voice (Wave 3, nice-to-have)** — Enable the `voice-call` plugin in OpenClaw config (`plugins.entries.voice-call.enabled`), then confirm `openclaw skills info voice-call --json` shows `"eligible": true`. Re-run doctor + release gate to confirm **no regression** on core grades.

**Refresh protocol** — After pulling repo changes: `cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/` → `openclaw gateway restart` → `openclaw skills check`.

---

## 10. Lockstep + Alexa (strict channel sync)

**Goal:** One **task timeline** for Telegram, Alexa, and Cursor—no contradictory “I don’t have that skill” answers; ground truth is `openclaw skills list` / `skills info` **plus** the lockstep event log.

**Run the bus (local-first)**

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_SYNC_TELEGRAM_SECRET='long-random'
export ANDREA_SYNC_INTERNAL_TOKEN='long-random'
python3 scripts/andrea_sync_server.py
```

**Telegram:** Point BotFather `setWebhook` to  
`https://your-public-host/v1/telegram/webhook?secret=...` (same value as `ANDREA_SYNC_TELEGRAM_SECRET`) or use the header secret path. The handler returns `200` immediately, creates a task, routes simple turns through Andrea directly, and sends delegated work into the OpenClaw hybrid lane first. OpenClaw can then finish the task itself or escalate to Cursor via `cursor_handoff`.

**Addressing rules:** `@Andrea` prefers the direct Andrea lane, `@Cursor` requests Cursor-first collaboration, `@Andrea @Cursor` or phrasing like `work together` / `double-check` requests joint OpenClaw + Cursor handling, and `@Gemini` / `@Minimax` / `@OpenAI` / `@GPT` request a preferred OpenClaw model lane. Add phrases like `show the full dialogue`, `show all handoffs`, or `visible collaboration` when you want a much richer Telegram collaboration stream for an intentional sprint session.

**E2E helper (cloudflared + setWebhook + verify):** see [ANDREA_TELEGRAM_LOCKSTEP_E2E.md](ANDREA_TELEGRAM_LOCKSTEP_E2E.md) and `python3 scripts/andrea_lockstep_telegram_e2e.py tunnel-and-webhook`. The helper now uses the same env precedence as `scripts/andrea_sync_server.py` and reports `webhook_health` so you can tell the difference between `healthy`, `drifted`, and `unset`.

**Tri-LLM sprint mode:** for an aggressive one-hour Telegram-visible collaboration session, use [ANDREA_TELEGRAM_TRI_LLM_SPRINT.md](ANDREA_TELEGRAM_TRI_LLM_SPRINT.md). That guide explains how to request full collaboration visibility, how OpenClaw should use Gemini/Minimax/OpenAI by strength, and how Cursor fits into the execution lane.

**Alexa:** Publish a Custom Skill whose public endpoint is your cloud edge, and have that edge forward into `https://your-private-or-local-host/v1/alexa`. Set `ANDREA_SYNC_ALEXA_EDGE_TOKEN` on both sides so the local Andrea endpoint only accepts forwarded edge traffic. The repo includes a reference Lambda-style forwarder at `scripts/alexa_edge_lambda.py`. For the human setup walkthrough, use [ANDREA_ALEXA_USER_SETUP.md](ANDREA_ALEXA_USER_SETUP.md). For the lower-level integration details, see [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md) and [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md).

**Delegated lifecycle:** Built-in Telegram execution now appends lifecycle automatically for both OpenClaw-only runs and OpenClaw-to-Cursor escalations. For manual or external runs, you can still emit events directly:

```bash
export ANDREA_SYNC_URL=http://127.0.0.1:8765
export ANDREA_SYNC_INTERNAL_TOKEN=...
python3 scripts/andrea_sync_cursor_report.py --task-id tsk_... --event JobCompleted --payload '{"summary":"shipped"}'
```

**Doctor (optional):** `ANDREA_SYNC_DOCTOR=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_doctor.sh`  
Strict: also set `ANDREA_SYNC_REQUIRED=1` so a dead bus fails the doctor run.

**Incident recovery**

1. Confirm process: `curl -sS "$ANDREA_SYNC_URL/v1/health"`.
2. If DB corrupt, move aside `data/andrea_sync.db` and restart (loses history; last resort).
3. Replay Telegram updates only after fixing idempotency keys—duplicates should `CommandDeduped`, not double-run side effects.

**Alexa checklist**

1. Set `ANDREA_SYNC_ALEXA_EDGE_TOKEN` on the local Andrea server.
2. Set `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM=1` and either `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID` or `TELEGRAM_CHAT_ID`.
3. Confirm your cloud edge validates Alexa requests before forwarding and allowlists the expected skill application id.
4. Send a sample `/v1/alexa` request locally, then validate from the Alexa iPhone app first.
5. Repeat the same phrases on Fire TV Cube / Fire Stick only after the iPhone pass is stable.
6. Confirm delegated Alexa tasks create only one Telegram summary message, not progress spam.
