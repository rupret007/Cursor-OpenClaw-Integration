# OpenClaw + OpenAI: “wrong key” / auth failures

## Use the right kind of key

| Source | Works for `openai/*` (platform API) |
|--------|-----------------------------------|
| **[platform.openai.com → API keys](https://platform.openai.com/api-keys)** | Yes — must be **`sk-...`** (often **`sk-proj-...`** for project keys) |
| **ChatGPT Plus subscription** | **No** — that is not an API key |
| **Codex / ChatGPT OAuth path** | Different flow — use **`openai-codex`**, not the platform key paste |

OpenClaw docs: [OpenAI provider](https://docs.openclaw.ai/providers/openai).

## Recommended fix (platform API key from this repo’s `.env`)

1. Put the key in **repo** `.env` (and skill `.env` if you like):

   ```bash
   python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill
   ```

2. **Validate shape** (does not print the key):

   ```bash
   bash scripts/openclaw_apply_openai_key.sh --dry-run
   ```

3. **Register the key with OpenClaw** (reads `.env`, never echoes the key):

   ```bash
   bash scripts/openclaw_apply_openai_key.sh
   ```

   This runs: `openclaw onboard --openai-api-key "$OPENAI_API_KEY"` as documented upstream.

4. **Restart gateway** and probe:

   ```bash
   openclaw gateway restart
   # --probe-timeout is in MILLISECONDS (e.g. 30s → 30000)
   openclaw models status --probe --probe-timeout 30000 --probe-concurrency 1
   ```

   Security / redaction when sharing logs: [ANDREA_SECURITY.md](ANDREA_SECURITY.md).

## Interactive alternative

```bash
cd /path/to/Cursor-OpenClaw-Integration
set -a && source .env && set +a
openclaw onboard --auth-choice openai-api-key
```

## If it still fails

- **Billing**: API keys need a **paid/billing-enabled** OpenAI **platform** project (check [limits / billing](https://platform.openai.com/settings/organization/billing)).
- **Wrong onboarding branch**: If you picked **OpenAI Codex (OAuth)** in the wizard, OpenClaw expects **ChatGPT/Codex sign-in**, not a raw **`sk-`** key — switch flow or use **`openai-api-key`** / script above.
- **Organization restrictions**: Some orgs disable standard API keys; check OpenAI dashboard policies.
- **Version**: `openclaw --version` — update OpenClaw if onboarding flags are unrecognized.

## Do not paste `/approve ... paste-token ...` in Terminal

That pattern is for **chat approval** flows. In Terminal use **`openclaw onboard`** or **`openclaw models auth ...`** per `openclaw --help`.
