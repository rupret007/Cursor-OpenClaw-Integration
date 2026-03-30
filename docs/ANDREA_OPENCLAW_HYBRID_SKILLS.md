# OpenClaw hybrid personal-assistant skills (Apple + Google + execution)

This doc supports the **Masterclass / hybrid expansion**: enable productivity lanes first, then execution/automation, then optional voice — without destabilizing the core OpenClaw baseline.

**Telegram / Andrea sync:** delegated work is executed through the **OpenClaw hybrid** lane only. The legacy **direct Cursor runner** (`_run_cursor_job` / `direct_cursor` polling) is no longer used for user-initiated jobs; `@cursor` mentions still influence collaboration *mode* copy, but the runtime handoff is OpenClaw-first.

**Hybrid prompt (`scripts/andrea_sync_openclaw_hybrid.py`):** Andrea shells out to `openclaw agent --json` with a structured system prompt that prioritizes **OpenClaw skills** (calendar, messaging, `cursor_handoff` for repo/PR work, etc.) and ends with a single-line `LOCKSTEP_JSON` contract. Keep this file aligned with whatever your installed `openclaw` CLI supports (flags, session IDs, thinking presets).

## Verification commands (after each wave)

```bash
cd /path/to/Cursor-OpenClaw-Integration
openclaw skills check
OPENCLAW_ENFORCE=1 MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
bash scripts/andrea_release_gate.sh
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

**Strict eligibility** (requires `jq` + satisfied skill requirements): set `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` to a CSV of skill keys, then run the enforcer — see `.env.example` comments.

Remove the **GitHub Grade‑C blocker** first when possible: `gh auth login` (or `GH_TOKEN` / `GITHUB_TOKEN` in `.env`).

---

## Wave 1 — Productivity (Apple + Google + meta)

| Skill | Skill key | Typical install / auth |
|------|-----------|-------------------------|
| BlueBubbles / iMessage | `bluebubbles` | See **[BlueBubbles + Andrea (OpenClaw)](#bluebubbles--andrea-openclaw)** below for the full runbook. |
| Apple Notes | `apple-notes` | Install **`memo`** CLI (Homebrew: `brew install memo` — confirm with `openclaw skills info apple-notes`). Grant macOS automation/Notes permissions as prompted. |
| Apple Reminders | `apple-reminders` | Install **`remindctl`** (common tap: `brew install steipete/tap/remindctl`; always verify with `openclaw skills info apple-reminders`). |
| Things 3 | `things-mac` | Install the **`things`** CLI for Things 3 per upstream instructions (`openclaw skills info things-mac` — may not be in core Homebrew). |
| Google Workspace | `gog` | Install **`gog`** CLI; complete its OAuth / config flow per upstream docs (`openclaw skills info gog`). |
| Summarize | `summarize` | Install **`summarize`** binary per skill metadata (`openclaw skills info summarize`). |
| Session logs | `session-logs` | Requires **`jq`** + **`rg`** (ripgrep) on `PATH` (`openclaw skills info session-logs`). |

**Observability:** `python3 scripts/andrea_capabilities.py --json` includes `skill:*` rows for hybrid skills. Missing requirements surface as **`ready_with_limits`**, not silent **`ready`**.

---

## BlueBubbles + Andrea (OpenClaw)

BlueBubbles is the **primary OpenClaw skill** for **personal iMessage / phone texting** in this stack: capability answers, **recent texts** (structured OpenClaw fetch), and **outbound draft → confirm** when the lane is verified. Andrea resolves it via `MESSAGING_SKILL_CANDIDATES` in [`services/andrea_sync/server.py`](../services/andrea_sync/server.py); delegated messaging-heavy asks should still go through OpenClaw with the **`bluebubbles`** skill when the user means the phone lane, not Telegram-in-chat.

### Install and verify

```bash
cd /path/to/Cursor-OpenClaw-Integration
openclaw skills info bluebubbles
openclaw skills install bluebubbles   # if not already installed
openclaw skills check
openclaw gateway restart
python3 scripts/andrea_capabilities.py --json | rg -i bluebubbles || true
OPENCLAW_ENFORCE=1 MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
```

To **require** BlueBubbles in strict checks, include `bluebubbles` in `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` (see [.env.example](../.env.example)).

### Bridge and macOS

- Keep the **BlueBubbles server/bridge** running and reachable from the machine where **OpenClaw** runs.
- Grant **Automation**, **Full Disk Access**, or other prompts the skill metadata describes; without them, `openclaw skills info bluebubbles` may show limits and Andrea will surface **installed but not eligible** / unavailable copy instead of implying send/read works.

### What Andrea does today

| User intent | Path | Notes |
|-------------|------|--------|
| “Can you read my iMessages / BlueBubbles?” | Direct structured reply | Read-focused capability answer; may mention drafting outbound separately. |
| “Recent texts / messages today …” | `_fetch_recent_text_messages` → OpenClaw | Uses ephemeral session guardrails where configured; failures stay user-safe. |
| “Tell Candace …” / text-to-person | Outbound draft → `send it` / `cancel` | BlueBubbles is the default messaging skill key when matched; WhatsApp uses `wacli` when the user asks for WhatsApp. |
| Stripped `@openclaw` + “Tell to …” | Not treated as SMS recipient `to` | Parser rejects invalid outbound targets so todo-style asks are not drafted as texts. |

### What we do not promise

- **Telegram** chat content is not “your iPhone texts” unless the user clearly asks about the **phone / iMessage** lane.
- No send or read guarantee if the bridge is down; trust **doctor + capabilities JSON**, not optimistic copy.

---

## Wave 2 — Execution and automation

| Skill | Skill key | Typical install / auth |
|------|-----------|-------------------------|
| Coding agents | `coding-agent` | At least one of **`claude`**, **`codex`**, **`opencode`**, **`pi`** on `PATH` (see `openclaw skills info coding-agent`). |
| Terminal mux | `tmux` | `brew install tmux` (or distro package). |
| UI capture | `peekaboo` | Install **`peekaboo`** per skill metadata (macOS). |
| Gemini CLI (optional) | `gemini` | Install **`gemini`** CLI if you want the CLI lane beyond API-only usage (`openclaw skills info gemini`). |

---

## Wave 3 — Voice (nice-to-have)

| Skill / plugin | Skill key | Notes |
|-----------------|-----------|--------|
| Voice calls | `voice-call` | Requires OpenClaw config: `plugins.entries.voice-call.enabled` (see `openclaw skills info voice-call --json`). |
| Local STT/TTS (optional) | `openai-whisper`, etc. | Only if you explicitly want offline audio; validate with `openclaw skills check` after install. |

**Success criteria:** stable call startup/teardown; **no regression** on doctor + release gate; keep a one-page runbook note in [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) §9.

---

## Recommended environment knobs

| Variable | Purpose |
|----------|---------|
| `ANDREA_REQUIRED_OPENCLAW_SKILLS` | CSV passed to `andrea_openclaw_enforce.sh` — default includes hybrid keys (catalog presence). |
| `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` | CSV; when non-empty, enforcer verifies each skill is **eligible** via `openclaw skills info <name> --json` + `jq`. |
| `ANDREA_OPENCLAW_SKILLS_CHECK` | Set to `1` to run `openclaw skills check` during enforce (informational; does not fail the script). |
| `ANDREA_OPENCLAW_DELEGATE_BIAS` | `standard` (default), `conservative` (same as standard today), or `aggressive` — sends more short/substantive questions to the **OpenClaw hybrid** lane instead of direct lookup. |

**Capability / “what can OpenClaw do” questions** (and similar phrasing) are routed to **delegated OpenClaw** with a skills-forward hybrid prompt, not the generic grounded web-lookup path.

---

## Refresh skill + gateway (after repo changes)

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills check
```
