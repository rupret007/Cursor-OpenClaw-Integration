# Cursor-OpenClaw-Integration

Hardened **Cursor Cloud Agents** integration toolkit for **OpenClaw**, shell workflows, and the **Andrea** lockstep assistant stack across Telegram and Alexa.

**Yes — OpenClaw is “in here”.** This repo includes OpenClaw-facing scripts and a vendored OpenClaw skill (`skills/cursor_handoff/`) that hands off heavy repo work to Cursor safely.

**Deployment:** use the **`main`** branch as the default production-style baseline. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Contents

- [What this repository provides](#what-this-repository-provides)
- [Repository layout](#repository-layout)
- [Admin setup (guided)](#admin-setup-guided)
- [Quick start](#quick-start)
- [OpenClaw skill (`cursor_handoff`)](#openclaw-skill-cursor_handoff)
- [Documentation](#documentation)
- [CLI reference](#cli-reference)
- [Testing](#testing)
- [Environment variables](#environment-variables)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## What this repository provides

- **CLI** (`scripts/cursor_openclaw.py`) for Cursor Cloud Agents API operations:
  - Auth / health: `diagnose`, `whoami`, `models`
  - Agent lifecycle: `create-agent`, `list-agents`, `agent-status`, `followup`, `stop-agent`, `delete-agent`
  - Insights: `conversation`, `artifacts`, `artifact-download-url`
- **Auth fallback:** `--auth-mode auto` tries bearer then basic when the API or docs disagree.
- **Resilience:** retries with backoff on `429`, `5xx`, and transient **network/SSL** failures.
- **Unicode:** request JSON uses UTF-8 (`ensure_ascii=False`) so prompts stay readable end-to-end.
- **OpenClaw skill** (`skills/cursor_handoff/`): API-first handoff with CLI fallback, diagnostics, dry-run, tests.
- **Andrea lockstep bus** (`services/andrea_sync/`): shared task/event timeline for Telegram, Alexa, OpenClaw, and Cursor with Andrea-first routing.
- **Planner/critic/executor trace:** machine-derived orchestration steps and user-safe collaboration summaries instead of raw runtime chatter.
- **Principal memory + reminders:** durable identity, memory notes, preferences, and scheduled follow-through across Telegram and Alexa.
- **Closed-loop local self-heal:** regression-backed optimization proposals and gated Cursor branch prep via `services/andrea_sync/optimizer.py` and `scripts/andrea_optimize.py`. **`LOCAL_AUTO_HEAL_COMPLETED` means post-handoff detached-worktree verification passed**, not merely that Cursor accepted the handoff (same contract as incident repair; tune with `ANDREA_SELF_HEAL_POST_CURSOR_VERIFY`).
- **Incident-driven repair loop:** Gemini triage, GPT mini patching, MiniMax challenge, GPT deep-debug planning, isolated verification, rollback, and optional Cursor escalation via `services/andrea_sync/repair_orchestrator.py` and `scripts/andrea_repair_cycle.py`.
- **Voice + chat coordination**: direct Andrea replies stay concise, delegated work runs through OpenClaw/Cursor, and Alexa sessions can mirror a single compact summary back to Telegram.

## Repository layout

```text
.
├── LICENSE
├── .env.example
├── README.md
├── docs/
│   ├── DEPLOYMENT.md                # main branch, env, gateway, verify
│   ├── OPENCLAW_SKILL.md            # install skill, typical flows
│   ├── CLI_REFERENCE.md             # flags and subcommands
│   ├── ANDREA_*.md                  # Andrea max-autonomy: matrix, policy, runbooks, playbook
│   ├── ANDREA_ALEXA_USER_SETUP.md   # user-facing Alexa app + Developer Console setup guide
│   └── ALEXA_CLOUD_EDGE_TEMPLATE.md # Alexa public-edge forwarding contract
├── services/
│   └── andrea_sync/           # lockstep bus, adapters, routing, HTTP server, formatting
├── openclaw-cursor-integration-proposal.md
├── openclaw-cursor-integration-roadmap.md
├── scripts/
│   ├── cursor_openclaw.py
│   ├── alexa_edge_lambda.py # reference Alexa cloud-edge forwarder with fallback response mapping
│   ├── cursor_api_common.py # shared validation, HTTP helpers (mirrored under skills)
│   ├── env_loader.py        # auto-load .env (used by CLIs)
│   ├── setup_admin.sh       # interactive .env + optional OpenClaw skill install
│   ├── exhaustive_feature_check.sh  # offline sweep of both CLIs (+ optional live API)
│   ├── andrea_capabilities.py      # Andrea runtime capability matrix (live readiness)
│   ├── andrea_readiness_grade.py   # A/B/C grade from capability JSON
│   ├── andrea_security_sanity.sh     # repo secret-pattern sanity checks
│   ├── andrea_slo_check.sh         # grade + optional OpenClaw model probe
│   ├── andrea_doctor.sh            # one-pass: security + grade + probes + probe
│   ├── andrea_autonomy_cycle.sh    # closed-loop local autonomy pass
│   ├── andrea_model_guard.sh       # automatic profile failover + reprobe loop
│   ├── andrea_openclaw_enforce.sh  # sync skill + required skills + probe/guard
│   ├── andrea_optimize.py          # optimization cycle + optional auto-heal branch prep
│   ├── andrea_experience_cycle.py  # deterministic UX replay + optional repair bridge
│   ├── andrea_repair_cycle.py      # incident-driven repair loop + optional Cursor escalation
│   ├── andrea_release_gate.sh      # STRICT security + grade not C + test_integration
│   ├── andrea_slo_telegram.sh      # timed Telegram getMe SLO (token from env only)
│   ├── handoff_context.py          # shared intent templates + repo triage text
│   ├── andrea_reliability_probes.sh # deterministic probes + capability snapshot
│   ├── dotenv_set_key.py     # merge one .env key without full wizard overwrite
│   ├── openclaw_apply_openai_key.sh  # openclaw onboard --openai-api-key from .env
│   ├── test_integration.sh
│   └── macos/                # LaunchAgents, post-login bootstrap, localtunnel helper
├── skills/
│   └── cursor_handoff/        # vendored skill (sync to ~/.openclaw/workspace/skills/)
│       ├── SKILL.md
│       ├── .env.example
│       ├── scripts/         # includes env_loader.py, cursor_api_common.py (mirror)
│       └── tests/
└── tests/
    ├── test_cursor_openclaw.py
    ├── test_cursor_api_common.py
    ├── test_andrea_sync.py
    ├── test_andrea_sync_http.py
    ├── test_andrea_full_cycle.py
    └── test_env_loader.py
```

## Admin setup (guided)

For a new machine or operator, run the interactive wizard (writes a **local** `.env`, mode `600`, ignored by git; never commit it):

```bash
bash scripts/setup_admin.sh
```

**Non-interactive (e.g. your own terminal, key already exported):** writes `.env`, syncs skill, restarts gateway, runs `diagnose`. Refuses to overwrite `./.env` unless you pass **`--force`**.

Use your real key; **paste only the commands** (not prose or `# …` comment lines from chat), or zsh may error.

```bash
export CURSOR_API_KEY="…"
# Optional batch-only:
# export OPENAI_API_KEY="…"
# export OPENAI_API_ENABLED=1   # or true | yes
# export GH_TOKEN="…"
# export GEMINI_API_KEY="…"
# export TELEGRAM_BOT_TOKEN="…"
# export TELEGRAM_CHAT_ID="…"
# export BRAVE_SEARCH_API_KEY="…"
# export BRAVE_ANSWERS_API_KEY="…"
# export MINIMAX_API_KEY="…"
# export SSL_CERT_FILE="…"   # optional TLS CA bundle path
```

```bash
bash scripts/setup_admin.sh --batch
```

If `./.env` already exists and you want to replace it:

```bash
bash scripts/setup_admin.sh --batch --force
```

**Persist a single secret without re-running the full wizard** (merges into `./.env`, keeps other keys; sets both `GH_TOKEN` and `GITHUB_TOKEN` by default):

```bash
python3 scripts/dotenv_set_key.py GH_TOKEN --skill
# hidden prompt on TTY, or:  python3 scripts/dotenv_set_key.py GH_TOKEN --value "$GH_TOKEN" --skill
```

**OpenAI (platform API key + enable flag for this repo’s CLIs/skills):**

```bash
python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill
openclaw gateway restart
```

See [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md) for how `OPENAI_API_ENABLED` gates usage and how this relates to OpenClaw’s own provider settings.

If OpenClaw rejects the key, see **[docs/OPENCLAW_OPENAI_TROUBLESHOOTING.md](docs/OPENCLAW_OPENAI_TROUBLESHOOTING.md)** and run **`bash scripts/openclaw_apply_openai_key.sh`** (uses `openclaw onboard --openai-api-key` per upstream docs).

It will:

- Prompt for **CURSOR_API_KEY** (hidden input) and optional **CURSOR_BASE_URL** / **CURSOR_AUTH_MODE**
- Optional **CURSOR_EMAIL** and **OPENCLAW_CURSOR_DEFAULT_MODE** (`auto` \| `api` \| `cli`) for the handoff skill
- Optional **OPENAI_API_KEY** (hidden) and **OPENAI_API_ENABLED** (`[y/N]`; no key forces disabled)
- **Optional integrations block** (`[Y/n]`): skip entirely for Cursor-only setups, or enter **GH_TOKEN** (also writes **GITHUB_TOKEN**), **GEMINI_API_KEY**, Telegram bot + chat id, Brave keys, **MINIMAX_API_KEY**, **SSL_CERT_FILE** — each field skippable with Enter
- Write **`./.env`** with `set -a && source .env && set +a` usage hints
- Optionally install **`cursor_handoff`** under `~/.openclaw/workspace/skills/` (replaces that folder if present), write **`~/.openclaw/workspace/skills/cursor_handoff/.env`**, restart **`openclaw gateway`**, and run **`diagnose`**

The CLIs read the **process environment**. They also **auto-load** a repo-root `.env` (and the skill directory `.env` for `cursor_handoff`) if present, **without** overriding variables you already exported.

Optional: load the same file in your shell:

```bash
cd /path/to/Cursor-OpenClaw-Integration
set -a && source .env && set +a
```

### Admin service control (macOS)

Use the canonical runtime control surface for everyday operator tasks:

```bash
bash scripts/andrea_services.sh status all
bash scripts/andrea_services.sh status sync
bash scripts/andrea_services.sh start all
bash scripts/andrea_services.sh restart all
bash scripts/andrea_services.sh stop all
bash scripts/andrea_services.sh bootstrap
```

`status sync` and `status webhook` now read the running daemon's own `/v1/runtime-snapshot` view of `ANDREA_SYNC_PUBLIC_BASE`, Telegram webhook health/drift, and capability-digest freshness instead of trusting the current shell environment.

For reboot-ready auto-start after login, install the LaunchAgents through the same surface:

```bash
bash scripts/andrea_services.sh install-launchagents --with-cloudflared --load
# Fallback when cloudflared is unavailable:
# bash scripts/andrea_services.sh install-launchagents --with-localtunnel --load
```

The recommended model is per-user LaunchAgents in `gui/$UID`, not system boot daemons. The legacy `--with-openclaw-refresh` agent remains available for compatibility, but the post-login bootstrap already restarts the gateway and now debounces duplicate login-time restarts.

## Quick start

### 1. Clone and use `main`

```bash
git clone https://github.com/rupret007/Cursor-OpenClaw-Integration.git
cd Cursor-OpenClaw-Integration
git checkout main && git pull origin main
```

### 2. Set your API key (do not commit)

**Easiest:** [Admin setup (guided)](#admin-setup-guided) — `bash scripts/setup_admin.sh`.

**Manual:**

```bash
read -s "CURSOR_API_KEY?Paste Cursor API key: "
echo
export CURSOR_API_KEY
```

If you only assign the variable without `export`, child processes (including Python) will not see it. If you use a `.env` file, the CLIs load it automatically from the repo (or skill) root; you can still `source .env` in the shell if you want non-Python tools to see the same variables.

### 3. Diagnostics

```bash
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json whoami
python3 scripts/cursor_openclaw.py --json models
```

### 4. Launch an agent (example)

```bash
python3 scripts/cursor_openclaw.py --json create-agent \
  --prompt "Read-only audit of top 5 risks" \
  --repository "https://github.com/owner/repo" \
  --ref main \
  --branch-name "cursor/risk-audit" \
  --auto-create-pr false \
  --poll-attempts 3
```

More examples are in [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

## OpenClaw skill (`cursor_handoff`)

**Daily sync (recommended):** from your clone of this repo (ideally `main`), copy the skill into the OpenClaw workspace and restart the gateway so changes load:

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills list   # expect cursor_handoff ready
```

That flow is enough for normal updates. If you ever remove or rename files inside the skill in git, you can do a clean replace first (`rm -rf ~/.openclaw/workspace/skills/cursor_handoff`) and then the same `cp -R` — either approach works; `cp -R` alone is fine day to day.

First-time install: ensure the directory exists — `mkdir -p ~/.openclaw/workspace/skills` — then use the same `cp -R` line.

Full steps and flow: [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md).

## Andrea (max-autonomy operator)

**Andrea** is the hardened operator profile for this stack: capability baseline, execute-first policy, DevOps/Telegram/productivity runbooks, and reliability probes.

| Doc | Purpose |
|-----|---------|
| [docs/ANDREA_OPERATIONS_PLAYBOOK.md](docs/ANDREA_OPERATIONS_PLAYBOOK.md) | **Start here** — autonomy scope, verification, recovery |
| [docs/ANDREA_CAPABILITY_MATRIX.md](docs/ANDREA_CAPABILITY_MATRIX.md) | Live readiness matrix (`scripts/andrea_capabilities.py`) |
| [docs/ANDREA_AUTONOMY_POLICY.md](docs/ANDREA_AUTONOMY_POLICY.md) | Execute-first + confirm-required boundaries |
| [docs/ANDREA_DEVOPS_RUNBOOK.md](docs/ANDREA_DEVOPS_RUNBOOK.md) | Task → branch → test → PR + GitHub fallbacks |
| [docs/ANDREA_COMMS_PRODUCTIVITY.md](docs/ANDREA_COMMS_PRODUCTIVITY.md) | Telegram + productivity routines |
| [docs/ANDREA_READINESS_REPORT.md](docs/ANDREA_READINESS_REPORT.md) | Readiness template / sign-off |
| [docs/ANDREA_SECURITY.md](docs/ANDREA_SECURITY.md) | Secrets, redaction, gateway token, rotation |
| [docs/ANDREA_MODEL_POLICY.md](docs/ANDREA_MODEL_POLICY.md) | Model profiles + fallbacks + rate limits |
| [docs/ANDREA_LOCKSTEP_ARCHITECTURE.md](docs/ANDREA_LOCKSTEP_ARCHITECTURE.md) | Telegram / Alexa / Cursor shared lockstep bus + SQLite store, including the Andrea-vs-OpenClaw conductor split |
| [docs/ANDREA_SYNC_RUNBOOK.md](docs/ANDREA_SYNC_RUNBOOK.md) | Lockstep maintenance notes for kill switch, reminders, autonomy loop, and migrations |
| [docs/ANDREA_TELEGRAM_LOCKSTEP_E2E.md](docs/ANDREA_TELEGRAM_LOCKSTEP_E2E.md) | Telegram webhook + `cloudflared` + `scripts/andrea_lockstep_telegram_e2e.py` |
| [docs/ANDREA_TELEGRAM_TRI_LLM_SPRINT.md](docs/ANDREA_TELEGRAM_TRI_LLM_SPRINT.md) | High-visibility one-hour Telegram collaboration sprint across OpenClaw multi-model reasoning and Cursor execution, including direct `@Gemini` / `@Minimax` / `@OpenAI` model-lane requests |
| [docs/ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](docs/ANDREA_LOCKSTEP_REVIEW_FINDINGS.md) | Lockstep awareness / kill-switch / webhook review notes |
| [docs/ANDREA_ALEXA_INTEGRATION.md](docs/ANDREA_ALEXA_INTEGRATION.md) | Alexa Custom Skill voice lane, Telegram session summaries, and rollout notes |
| [docs/ANDREA_ALEXA_USER_SETUP.md](docs/ANDREA_ALEXA_USER_SETUP.md) | Step-by-step Alexa app + Developer Console setup guide for actual users/operators |
| [docs/ALEXA_CLOUD_EDGE_TEMPLATE.md](docs/ALEXA_CLOUD_EDGE_TEMPLATE.md) | Recommended public-edge forwarding/auth contract for Alexa |

**Startup self-check:**

```bash
python3 scripts/andrea_capabilities.py
```

**Masterclass health (one command):** security sanity + capability snapshot + **A/B/C** grade + reliability probes + optional `openclaw models status --probe`. OpenClaw **`--probe-timeout` is in milliseconds** (e.g. 30s → `30000`).

```bash
bash scripts/andrea_doctor.sh
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
# STRICT_SECURITY=1 bash scripts/andrea_doctor.sh   # fail on backup warnings too
# MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh  # auto-remediate failed model probe
# OPENCLAW_ENFORCE=1 bash scripts/andrea_doctor.sh  # enforce OpenClaw baseline first
```

**Model guard** (explicit remediation loop across profiles):

```bash
bash scripts/andrea_model_guard.sh
# dry-run:
# bash scripts/andrea_model_guard.sh --dry-run
```

**OpenClaw baseline enforcer** (skill sync + required skills + model probe):

```bash
bash scripts/andrea_openclaw_enforce.sh
# dry-run:
# bash scripts/andrea_openclaw_enforce.sh --dry-run
```

**SLO gate** (grade + probe):

```bash
bash scripts/andrea_slo_check.sh
# Optional Telegram latency (needs TELEGRAM_BOT_TOKEN): TELEGRAM_SLO=1 bash scripts/andrea_slo_check.sh
```

**Pre-release gate** (strict security warnings fail, readiness not Grade C, full integration):

```bash
bash scripts/andrea_release_gate.sh
```

**Cursor handoff intents** (same templates as `create-agent --intent`): `code-review`, `refactor`, `release-notes`, `brief`; optional `--triage` on local repo — see [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) and `skills/cursor_handoff/SKILL.md`.

**Reliability probes** (deterministic env for `diagnose` + JSON shape checks):

```bash
bash scripts/andrea_reliability_probes.sh
# optional: RUN_LIVE_PROBES=1 for gh + openclaw
```

**Closed-loop autonomy cycle** (health + regressions + optimization proposals + incident-driven repair + gated local auto-heal + proactive sweep):

```bash
export ANDREA_SYNC_URL='http://127.0.0.1:8765'
export ANDREA_SYNC_INTERNAL_TOKEN='…'
bash scripts/andrea_autonomy_cycle.sh
# proposal generation only:
# ANDREA_AUTONOMY_AUTO_APPLY_READY=0 bash scripts/andrea_autonomy_cycle.sh
# skip the incident repair lane:
# ANDREA_AUTONOMY_INCIDENT_REPAIR=0 bash scripts/andrea_autonomy_cycle.sh
```

**Experience assurance replay** (deterministic routing/UX/capability scenarios, persisted in the dashboard, optional repair bridge):

```bash
python3 scripts/andrea_experience_cycle.py --repo "$PWD"
# create a repair incident automatically when a scenario regresses:
# python3 scripts/andrea_experience_cycle.py --repo "$PWD" --repair-on-fail
# keep it ephemeral:
# python3 scripts/andrea_experience_cycle.py --repo "$PWD" --no-save
```

The replay now covers both the direct lane and delegated OpenClaw/Cursor Telegram flows, including calm final-copy scoring, bounded orchestration checks, and unnecessary-Cursor-escalation regressions. The dashboard includes an `Experience` card plus `Experience Assurance` / `Experience Regressions` sections backed by the latest persisted replay run.

**Direct incident-driven repair cycle** (verification, minimal patch attempts, optional Cursor escalation):

```bash
python3 scripts/andrea_repair_cycle.py --repo "$PWD"
# allow deep Cursor execution after the lightweight attempts fail:
# python3 scripts/andrea_repair_cycle.py --repo "$PWD" --cursor-execute
# retry a saved incident:
# python3 scripts/andrea_repair_cycle.py --repo "$PWD" --incident-id inc_1234abcd
# inject a runtime error / health failure / log alert packet:
# python3 scripts/andrea_repair_cycle.py --repo "$PWD" --runtime-error-json data/sample_runtime_error.json
```

Heavy Cursor repair uses **bounded polling** on `cursor_handoff.py`. For **`backend=api`**, the repair orchestrator also **polls Cloud Agent status** after submission (same `ANDREA_REPAIR_CURSOR_POLL_*` knobs) until the agent reaches a **terminal** state before deciding whether to run **post-handoff verification** in a detached git worktree. An incident is only marked **`resolved`** after that verification passes. **`cursor_handoff_ready`** means the handoff succeeded but the fix is **not** auto-verified yet (verify skipped, still running, or waiting on you to monitor Cursor)—not “the bug is fixed.” **`human_review_required`** covers failed verification, non-`FINISHED` terminal Cursor states, or a missing branch for verify. Conductor metadata includes an **`outcome`** block (`submission_status`, `terminal_cursor_status`, `verification_status`, `next_action`, etc.) and **`handoff.plan_first_fallback_reason`** when plan-first was enabled but the planner path fell back to single-pass. Tune with `ANDREA_REPAIR_CURSOR_POLL_MAX_ATTEMPTS`, `ANDREA_REPAIR_CURSOR_POLL_INTERVAL_SECONDS`, `ANDREA_REPAIR_POST_CURSOR_VERIFY` (`0` disables post-handoff verify), and `ANDREA_REPAIR_WORKTREE_ROOT` / `ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS` as needed.

**Local auto-heal (optimizer)** reuses the same polling + verification semantics: `ApplyOptimizationProposal` only records **`LOCAL_AUTO_HEAL_COMPLETED`** when verification passes. Set `ANDREA_SELF_HEAL_POST_CURSOR_VERIFY=0` only when you intentionally want to allow “handoff succeeded” without detached verify (the apply result will fail closed with `self_heal_post_cursor_verify_disabled` when verify is required but off).

**Plan-first Cursor (optional):** repair and auto-heal can run a **read-only planner** agent (strong model via `ANDREA_CURSOR_PLANNER_MODEL` / lane overrides), extract a `## CursorExecutionPlan` section from the planner conversation, then launch a second **executor** agent with `ANDREA_CURSOR_EXECUTOR_MODEL` (default `default`). Enable with `ANDREA_CURSOR_PLAN_FIRST_ENABLED` and/or `ANDREA_REPAIR_CURSOR_PLAN_FIRST`, `ANDREA_SELF_HEAL_CURSOR_PLAN_FIRST`. Direct **Telegram → Cursor** can use `ANDREA_TELEGRAM_CURSOR_PLAN_FIRST` (off by default). `cursor_handoff.py` **`--mode`** selects API vs CLI **transport**; **`--model`** selects the Cloud Agents **model** id—do not confuse with `mode=auto` on the handoff script.

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | **`main` as deployment branch**, requirements, verify, gateway |
| [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md) | Skill install, typical OpenClaw → Cursor flow |
| [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) | Flags and subcommands for both CLIs |
| [docs/ANDREA_OPERATIONS_PLAYBOOK.md](docs/ANDREA_OPERATIONS_PLAYBOOK.md) | Operator playbook for lockstep, reboot-ready startup, Telegram, and Alexa |
| [docs/ANDREA_TELEGRAM_TRI_LLM_SPRINT.md](docs/ANDREA_TELEGRAM_TRI_LLM_SPRINT.md) | Copy-paste guide for the aggressive Telegram tri-LLM sprint mode and visible collaboration flow |
| [docs/ANDREA_ALEXA_INTEGRATION.md](docs/ANDREA_ALEXA_INTEGRATION.md) | Alexa invocation model, short voice replies, Telegram summary behavior |
| [docs/ANDREA_ALEXA_USER_SETUP.md](docs/ANDREA_ALEXA_USER_SETUP.md) | Human setup guide for getting AndreaBot working in the Alexa app and Developer Console |
| [docs/ALEXA_CLOUD_EDGE_TEMPLATE.md](docs/ALEXA_CLOUD_EDGE_TEMPLATE.md) | Thin public-edge template for Alexa request verification and forwarding |
| [openclaw-cursor-integration-roadmap.md](openclaw-cursor-integration-roadmap.md) | Phased integration plan |
| [openclaw-cursor-integration-proposal.md](openclaw-cursor-integration-proposal.md) | Design notes / ideas |

## CLI reference

See [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) for full tables. Cursor API: [Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints).

## Testing

**Integration CLI + skill (recommended):**

```bash
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

`test_integration.sh` runs **`scripts/andrea_security_sanity.sh`**, **`scripts/andrea_reliability_probes.sh`**, a non-fatal **`andrea_readiness_grade.py`** smoke, then **`scripts/exhaustive_feature_check.sh`** (every subcommand `--help`, validation paths, handoff diagnose/dry-run modes). Optional live API smoke:

```bash
RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
```

**Live operator cycle** (needs `andrea_sync` running + `ANDREA_SYNC_INTERNAL_TOKEN`):

```bash
export ANDREA_SYNC_INTERNAL_TOKEN='…'
export ANDREA_SYNC_URL='http://127.0.0.1:8765'
bash scripts/andrea_full_cycle.sh
```

See [docs/ANDREA_OPERATIONS_PLAYBOOK.md](docs/ANDREA_OPERATIONS_PLAYBOOK.md) for skip flags (`SKIP_GIT`, `SKIP_KILL_DRILL`, etc.).

**Andrea lockstep tests** (Telegram/Alexa routing, HTTP ingress, summaries):

```bash
python3 -m unittest discover -s tests -p 'test_andrea_sync*.py'
```

**Overnight / soak:** safe to loop `RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh` or your own agent workflows; avoid high `list-agents` limits or tight polling against production so you don’t hit rate limits.

**Exit codes (`cursor_openclaw.py`):** `0` success, `2` usage/validation error, `4` HTTP/API failure.

**Unit tests only:**

```bash
python3 -m py_compile scripts/cursor_openclaw.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s skills/cursor_handoff/tests -p 'test_*.py' -v
```

## Environment variables

See [.env.example](.env.example) and [skills/cursor_handoff/.env.example](skills/cursor_handoff/.env.example).

| Variable | Required | Notes |
|----------|----------|--------|
| `CURSOR_API_KEY` | For live API | Export in the shell that runs Python |
| `CURSOR_BASE_URL` | No | Default `https://api.cursor.com`; if set, must start with `http://` or `https://` |
| `CURSOR_AUTH_MODE` | No | `auto`, `basic`, `bearer` |
| `OPENAI_API_KEY` | No | Optional; API key from [OpenAI platform](https://platform.openai.com/). Does **not** use ChatGPT Plus — use an API key with billing enabled. |
| `OPENAI_API_ENABLED` | No | When `1`, `true`, or `yes` (case-insensitive), code paths that respect this flag may use `OPENAI_API_KEY`; otherwise the key is ignored. Use `bash scripts/setup_admin.sh` or `python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill`. See [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md). |
| `GH_TOKEN` / `GITHUB_TOKEN` | No | Optional GitHub token for `gh`/GitHub-related OpenClaw skills (if they use env-based auth). |
| `GEMINI_API_KEY` | No | Optional Gemini key for Gemini skills/CLIs. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No | Optional Telegram bot credentials (skill-dependent). |
| `ANDREA_SYNC_ALEXA_EDGE_TOKEN` | No | Recommended for Alexa rollout; shared secret between the public Alexa edge and local `/v1/alexa`. |
| `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM` / `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID` | No | Controls whether Alexa sessions mirror one summary to Telegram and which chat receives it. |
| `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED` | No | Global on/off switch for delegated Alexa/OpenClaw/Cursor execution. |
| `ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED` / `ANDREA_SYNC_PROACTIVE_SWEEP_INTERVAL_SECONDS` | No | Enables the server-side reminder sweep loop and controls how often due reminders are delivered. |
| `ANDREA_CURSOR_REPO` / `ANDREA_CURSOR_HANDOFF_MODE` | No | Default repo path and Cursor handoff mode for Telegram-triggered execution. |
| `ANDREA_SYNC_CURSOR_REPO` | No | Override repo path used by admin/autonomy helpers such as the local self-heal runner. |
| `ANDREA_SELF_HEAL_CURSOR_MODE` | No | Cursor backend override for auto-heal branch-prep proposals (`auto`, `api`, `cli`). |
| `ANDREA_AUTONOMY_INCIDENT_REPAIR` / `ANDREA_AUTONOMY_INCIDENT_CURSOR_EXECUTE` | No | Controls whether `andrea_autonomy_cycle.sh` runs the incident repair loop and whether deep repair plans may auto-escalate into Cursor. |
| `ANDREA_SYNC_BACKGROUND_INCIDENT_REPAIR_ENABLED` / `ANDREA_SYNC_BACKGROUND_INCIDENT_CURSOR_EXECUTE` | No | Lets the idle background optimizer optionally run the incident repair loop and, if desired, allow Cursor escalation. |
| `ANDREA_SYNC_BACKGROUND_REGRESSION_MAX_AGE_SECONDS` | No | Max age (default `172800` = 48h) for the **latest persisted experience assurance run** to count as fresh evidence for the idle background optimizer. Older runs close the autonomy gate and force **heuristic-only** optimizer ticks (no Gemini background bundle). |
| `ANDREA_REPAIR_ENABLED` / `ANDREA_REPAIR_CURSOR_MODE` | No | Global on/off switch for the incident repair control plane and the deep Cursor handoff backend (`auto`, `api`, `cli`). |
| `ANDREA_REPAIR_AUTO_CURSOR_HEAVY` | No | When `1`/`true`, auto-run Cursor handoff on deep escalation when the repair plan is heavy and the main worktree is clean (without requiring `cursor_execute`). |
| `ANDREA_REPAIR_CURSOR_POLL_MAX_ATTEMPTS` / `ANDREA_REPAIR_CURSOR_POLL_INTERVAL_SECONDS` | No | Bounded synchronous polling for Cursor handoff status (defaults: a few attempts with a short interval). |
| `ANDREA_REPAIR_POST_CURSOR_VERIFY` | No | When `1` (default), after a successful Cursor handoff with a resolvable branch, run verification in an isolated worktree before marking the incident resolved. |
| `ANDREA_SELF_HEAL_POST_CURSOR_VERIFY` | No | Override for the **optimizer / auto-heal** lane: when unset, follows `ANDREA_REPAIR_POST_CURSOR_VERIFY`. When `0`/`false`, auto-heal apply fails closed unless you explicitly accept handoff-only semantics. |
| `ANDREA_REPAIR_PROMPT_VERSION` + per-role `ANDREA_REPAIR_*_PROMPT_VERSION` | No | Pins prompt contracts for triage, primary patching, challenger patching, deep planning, and Cursor handoff artifacts. |
| `ANDREA_REPAIR_SAFE_ROOTS` / `ANDREA_REPAIR_MAX_PATCH_ATTEMPTS` | No | Overrides the repo-safe auto-repair roots and the number of lightweight patch attempts before escalation. |
| `ANDREA_REPAIR_MAX_MODEL_INVOCATIONS` / `ANDREA_REPAIR_MAX_CHANGED_LINES` | No | Budget controls for per-incident model usage and patch scope. |
| `ANDREA_REPAIR_STRICT_MODEL_MATCH` | No | When `1`, fail a repair lane if the reported provider/model does not match the requested routing hints. |
| `BRAVE_SEARCH_API_KEY` / `BRAVE_ANSWERS_API_KEY` | No | Optional Brave Search skill keys (`brave-api-search` expects both names; answers key may reuse search key). |
| `MINIMAX_API_KEY` | No | Optional MiniMax provider key for MiniMax integrations. |
| `SSL_CERT_FILE` | No | Optional path to CA bundle for Python TLS (macOS `CERTIFICATE_VERIFY_FAILED`); see README troubleshooting. |

**Idle background optimizer trust:** When `ANDREA_SYNC_BACKGROUND_OPTIMIZER_ENABLED=1`, the server **does not** inject a synthetic passing regression report. The autonomy gate uses the latest **`experience_runs`** row from `python3 scripts/andrea_experience_cycle.py` (or any caller that persists via the same store). Without a fresh run, proposals stay **`gated`**, Gemini/MiniMax/OpenAI background lanes are skipped, and optional background incident repair only runs when the latest experience snapshot is **fresh** (and passes the gate’s skill/digest checks as before). See dashboard **`Bg autonomy`** and `GET /v1/dashboard/summary` → **`background_autonomy`**. Task rows include **`delegated_lifecycle`** (unified OpenClaw/Cursor contract) and **`resource_lane`** / **`verification_story`** for scripting.

## Security

- Never commit API keys or paste them into assistant chats.
- Full operator checklist: **[docs/ANDREA_SECURITY.md](docs/ANDREA_SECURITY.md)** — env-first secrets, redaction-safe diagnostics, gateway token rotation, `bash scripts/andrea_security_sanity.sh`.
- `diagnose` redacts Cursor and OpenAI keys by default; avoid `--show-key` in shared logs.
- Prefer short-lived keys and rotate if exposed.
- Treat agent outputs and artifact URLs as sensitive until reviewed.

## Troubleshooting

| Symptom | What to try |
|---------|----------------|
| `CURSOR_API_KEY missing` in Python | Use `export CURSOR_API_KEY=...`, or create `./.env` (wizard writes one; CLIs auto-load it if the key is not already set in the environment). |
| `401 Unauthorized` | Wrong key type or revoked key; confirm key in Cursor settings. |
| `CERTIFICATE_VERIFY_FAILED` (Python) | On macOS, try `export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())')"` if `certifi` is installed. |
| Skill not listed after copy | `openclaw gateway restart`; confirm path `~/.openclaw/workspace/skills/cursor_handoff/SKILL.md`. |
| `acpx` missing even though `acp-router` is loaded | Install it with `npm install -g acpx`, then `openclaw gateway restart` so the ACP router lane can launch sessions. |
| zsh: `command not found: #` / `no matches found` after paste | You pasted comment lines or broken lines into the shell. Run commands one at a time; avoid copying `#` comment lines from docs or chat. |
| `create-agent` validation errors | Use `--dry-run` first; use **either** `--repository` or `--pr-url`, not both; check `--ref` / `--branch-name` per API docs. |
| `Invalid --id format` | Pass only the agent id (e.g. `bc-…`), not a full URL. Allowed characters: letters, digits, `._:-`. |
| `Base URL must start with http:// or https://` | Fix `CURSOR_BASE_URL` / `--base-url` (no `ftp://`, bare hostnames, etc.). |

## Hardening details (summary)

- `--auth-mode auto` tolerates bearer vs basic inconsistencies.
- `--retries` + exponential backoff reduce transient failures (including transport-layer errors).
- `diagnose` redacts secrets.
- `create-agent --dry-run` validates payload without network calls.
- `cursor_handoff` supports `--dry-run` and read-only defaults for safer delegation.

## License

[MIT](LICENSE).
