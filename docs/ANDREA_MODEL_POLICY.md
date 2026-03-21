# Andrea model policy (OpenClaw)

Structured **profiles** so Andrea can switch behavior without ad-hoc guesswork. OpenClaw resolves **primary + fallbacks** in order; session commands like `/model` override per chat.

---

## 1. Recommended profiles

| Profile | Primary (example) | Intent |
|---------|-----------------|--------|
| **fast** | `google/gemini-2.5-flash` | Low latency, everyday tasks |
| **balanced** | `google/gemini-2.5-flash` + fallbacks `openai/gpt-5.3-codex`, `minimax/MiniMax-M2.5` | Default “personal assistant” |
| **deep** | `openai/gpt-5.3-codex` or `google/gemini-3.1-pro-preview` | Harder reasoning / coding (higher cost) |

Adjust ids to match `openclaw models list --provider <name> --all --plain` on your machine.

---

## 2. Apply a profile (CLI)

**Balanced (example — your current baseline):**

```bash
openclaw models set google/gemini-2.5-flash
openclaw models fallbacks clear
openclaw models fallbacks add openai/gpt-5.3-codex
openclaw models fallbacks add minimax/MiniMax-M2.5
openclaw gateway restart
```

**Deep (OpenAI-first):**

```bash
openclaw models set openai/gpt-5.3-codex
openclaw models fallbacks clear
openclaw models fallbacks add google/gemini-2.5-flash
openclaw models fallbacks add minimax/MiniMax-M2.5
openclaw gateway restart
```

---

## 3. Telegram / session switching (no config rewrite)

- `/model list` — picker
- `/model openai/gpt-5.3-codex` — direct
- `/model GPT` — if alias `GPT` points at your preferred OpenAI model
- `/model flash` / `/model gemini-flash` — if you configured aliases

---

## 4. Rate limits & cooldown playbook

When you see `rate_limit`, `cooldown`, or `model_not_found`:

1. **Confirm probe timeout units** — OpenClaw uses **milliseconds**: e.g. 30s → `--probe-timeout 30000`.
2. **Let fallbacks run** — ensure `openclaw models fallbacks list` includes a healthy second provider.
3. **Session escape hatch** — `/model minimax` or `/model GPT` to leave a hot provider.
4. **Reduce load** — shorter prompts, fewer parallel automations, backoff 2s / 5s / 15s.
5. **Log slice** — `openclaw logs --follow` (redact before sharing).

---

## 5. Automatic remediation (model guard)

To enforce policy recovery (not just document it), use:

```bash
bash scripts/andrea_model_guard.sh
```

Behavior:

- Tries profile order (default: `balanced,fast,deep`)
- Applies model + fallbacks for each profile
- Probes with OpenClaw (`--probe-timeout` in **ms**)
- Stops on first successful profile, otherwise exits non-zero

Useful forms:

```bash
# Safe preview (no openclaw mutation):
bash scripts/andrea_model_guard.sh --dry-run

# Custom order and timeout:
bash scripts/andrea_model_guard.sh --order "fast,balanced,deep" --probe-timeout-ms 20000
```

Optional integration with doctor:

```bash
MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
```

If the model probe fails, doctor invokes model guard for automatic recovery.

---

## 6. Allowlist vs catalog

`agents.defaults.models` in `~/.openclaw/openclaw.json` acts as an **allowlist** for many flows. To expose **more** OpenAI models, add each `openai/...` id you want, or widen policy per [OpenClaw models concepts](https://docs.openclaw.ai/concepts/models).

---

## 7. Related

- [ANDREA_DEVOPS_RUNBOOK.md](ANDREA_DEVOPS_RUNBOOK.md)
- [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md)
- [ANDREA_SECURITY.md](ANDREA_SECURITY.md)
