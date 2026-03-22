# AndreaBot Alexa setup guide

This is the **human-friendly setup guide** for getting AndreaBot talking through Alexa.

Goal:

- you say `Alexa, ask Andrea Bot ...`
- Alexa speaks a short Andrea reply
- deeper work still goes through Andrea -> OpenClaw -> Cursor when needed
- Telegram receives one compact summary for the Alexa session so everything stays in sync

This guide is written for the real-world path:

`Alexa Custom Skill -> public HTTPS edge -> local Andrea backend`

If you want the lower-level integration details, see:

- [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md)
- [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md)

## What you need

Before you start, make sure you have:

- a working local Andrea stack
- `andrea_sync` running on your machine
- Telegram already configured if you want Alexa session summaries mirrored there
- an Amazon developer account
- the Alexa app on your iPhone for the first round of testing
- a public HTTPS endpoint you control

Recommended local health check first:

```bash
curl -sS http://127.0.0.1:8765/v1/health | python3 -m json.tool
```

You should see `"ok": true`.

## Masterclass architecture choice

Do **not** make your home machine the primary Alexa endpoint unless you have a very deliberate reason.

Recommended setup:

1. Alexa sends requests to a small public HTTPS edge.
2. That edge verifies Alexa traffic.
3. The edge forwards the raw Alexa JSON to your local Andrea backend.
4. Andrea decides whether to answer directly or bring in OpenClaw/Cursor.

Why this is the best path:

- stronger security boundary
- better certification readiness
- easier debugging
- safer if you later add account linking
- keeps Andrea as the single brain for routing and memory

## Step 1. Prepare the local Andrea backend

Set the Alexa-related env vars on the machine running `andrea_sync`.

Recommended:

```bash
export ANDREA_SYNC_ALEXA_EDGE_TOKEN='long-random-secret'
export ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM=1
export ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID='YOUR_TELEGRAM_CHAT_ID'
export ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED=1
```

Then restart the backend:

```bash
launchctl kickstart -k gui/$(id -u)/com.andrea.andrea-sync
sleep 5
curl -sS http://127.0.0.1:8765/v1/health | python3 -m json.tool
```

What these do:

- `ANDREA_SYNC_ALEXA_EDGE_TOKEN`
  protects `/v1/alexa` so only your cloud edge can forward requests
- `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM=1`
  enables one Telegram summary per Alexa task/session
- `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID`
  controls which Telegram chat gets the summary
- `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED=1`
  allows delegated Alexa work to continue into OpenClaw/Cursor

If you omit `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID`, Andrea falls back to `TELEGRAM_CHAT_ID`.

Notes:

- Alexa summaries are enabled by default today
- set `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM=0` only if you intentionally want no Telegram mirror

## Step 2. Stand up the public HTTPS edge

Your Alexa skill should point to a public HTTPS endpoint such as:

- AWS Lambda + API Gateway
- another HTTPS edge you control

That edge should:

- accept Alexa POST requests
- validate the request came from Alexa
- forward the JSON body to:
  - `https://YOUR_ANDREA_HOST/v1/alexa`
- attach one of:
  - `Authorization: Bearer $ANDREA_SYNC_ALEXA_EDGE_TOKEN`
  - `X-Andrea-Alexa-Edge-Token: $ANDREA_SYNC_ALEXA_EDGE_TOKEN`

Use this repo’s template:

- [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md)

## Step 3. Create the Alexa Custom Skill

In the Alexa Developer Console:

1. Create a new **Custom** skill.
2. Pick a skill name users will recognize.
3. Choose an invocation name that is natural to say.

Suggested invocation name:

- `andrea bot`

That gives you the natural phrase:

- `Alexa, ask Andrea Bot how are you`

Naming rule:

- skill display name can stay `AndreaBot`
- invocation name should be the spoken form `andrea bot`
- keep the spoken phrase consistent across the Alexa app, docs, and testing

Tip:

- keep the invocation easy to pronounce
- avoid names Alexa commonly confuses with other words

## Step 4. Configure the interaction model

Create one main intent for free-form requests.

Suggested intent:

- `AndreaCaptureIntent`

Suggested slot:

- `utterance`
- type: `AMAZON.SearchQuery`

This is the simplest and most flexible model for AndreaBot because it lets you pass natural language through with minimal friction.

Example sample utterances:

- `tell AndreaBot {utterance}`
- `ask AndreaBot {utterance}`
- `tell Andrea {utterance}`
- `{utterance}`

Recommended early test phrases:

- `how are you today`
- `remind me to stretch later`
- `summarize what I need to do this afternoon`
- `review the repo and fix the failing tests`

## Step 5. Configure the endpoint

In the Alexa Developer Console endpoint settings:

- choose your custom HTTPS endpoint
- enter your public edge URL

Example:

```text
https://your-edge.example.com/alexa
```

That public edge then forwards internally to:

```text
https://your-private-or-local-host/v1/alexa
```

## Step 6. Decide whether to use account linking

### Single-user / personal household path

For an initial personal setup, you can ship v1 **without account linking** if:

- this is primarily for you
- you trust the devices/account in use
- you do not need per-user identity beyond Alexa `userId`

### Multi-user / safer long-term path

Use **account linking** when:

- multiple people may use the skill
- you want stronger user identity guarantees
- you plan to expose personal data or personalized actions

Recommended choice:

- **authorization code grant**

That is the stronger and more future-proof route according to Amazon’s guidance.

Important notes:

- account linking is optional for a personal first rollout
- it becomes much more important for multi-user or more sensitive features
- if you add it, the edge/backend should validate the access token before using it

## Step 7. Validate the skill locally before device testing

First verify the Andrea backend path manually:

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/alexa \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ANDREA_SYNC_ALEXA_EDGE_TOKEN" \
  -d '{
    "session":{"sessionId":"amzn-session-demo"},
    "request":{
      "type":"IntentRequest",
      "requestId":"amzn-request-demo",
      "intent":{
        "name":"AndreaCaptureIntent",
        "slots":{"utterance":{"value":"how are you today"}}
      }
    }
  }'
```

Expected behavior:

- Alexa endpoint returns JSON with a short spoken response
- a new Alexa task appears in `GET /v1/tasks`
- the task contains Alexa metadata under `meta.alexa`

Check recent tasks:

```bash
curl -sS "http://127.0.0.1:8765/v1/tasks?limit=10" | python3 -m json.tool
```

## Step 8. Test from the Alexa iPhone app first

This is the recommended first live-device test path.

Why:

- quickest feedback loop
- easiest place to inspect enablement/account state
- simpler than debugging a living-room device first

Test order:

1. Enable the skill in the Alexa app.
2. Invoke it with a simple phrase:
   - `Alexa, ask Andrea Bot how are you today`
3. Try one assistant-style action:
   - `Alexa, ask Andrea Bot remind me to stretch at 4 PM`
4. Try one heavier request:
   - `Alexa, ask Andrea Bot review the repo and fix the failing tests`

What should happen:

- simple turn:
  - Alexa replies immediately in Andrea’s voice style
- heavier turn:
  - Alexa gives a short acknowledgement
  - Andrea/OpenClaw/Cursor do the heavier work behind the scenes
  - Telegram receives one compact summary when done

If heavier work never starts, confirm:

- `ANDREA_SYNC_BACKGROUND_ENABLED=1`
- `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED=1`
- OpenClaw and Cursor are healthy on the host

## Step 9. Roll out to Fire TV Cube / Fire Stick

Once the iPhone path feels stable:

1. Test the same phrases on Fire TV Cube or Fire Stick.
2. Verify the spoken pacing still feels natural.
3. Confirm Telegram still gets only one summary for the Alexa task.
4. Confirm you are not hearing technical status spam from Alexa.

Success criteria:

- Alexa sounds helpful, not verbose
- Andrea remains the narrator
- heavy work is invisible unless needed
- Telegram stays synchronized

## Good phrases to use

These tend to fit the current AndreaBot voice model well:

- `Alexa, ask Andrea Bot how are you`
- `Alexa, ask Andrea Bot what should I focus on next`
- `Alexa, ask Andrea Bot remind me to call dad tonight`
- `Alexa, ask Andrea Bot summarize what we decided earlier`
- `Alexa, ask Andrea Bot review the repo and fix the failing tests`

## Troubleshooting

### Alexa says the skill is not responding

Check:

- your public edge is live
- the edge can reach `/v1/alexa`
- the edge is forwarding the bearer token/header
- `andrea_sync` is healthy

Useful commands:

```bash
curl -sS http://127.0.0.1:8765/v1/health | python3 -m json.tool
curl -sS http://127.0.0.1:8765/v1/status | python3 -m json.tool
```

### Alexa can reach the skill, but nothing shows in Telegram

Check:

- `TELEGRAM_BOT_TOKEN`
- `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM` is not disabled
- `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID` or `TELEGRAM_CHAT_ID`

### Alexa works for simple requests but heavy work never finishes

Check:

- OpenClaw gateway health
- `cursor_handoff` skill health
- Cursor credentials
- `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED=1`
- `bash scripts/andrea_full_cycle.sh`

### Alexa keeps talking too much

That is not the intended UX.

AndreaBot should:

- keep the spoken answer short
- avoid narrating every status transition
- place the richer detail in Telegram

If that behavior regresses, re-check:

- [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md)
- the Alexa summary behavior
- direct-vs-delegated routing tests

## Security checklist

For a serious long-term rollout, treat this checklist as required:

1. Validate Alexa request signatures at the public edge.
2. Use a valid trusted HTTPS certificate.
3. Protect the local `/v1/alexa` endpoint with `ANDREA_SYNC_ALEXA_EDGE_TOKEN`.
4. Prefer account linking if multiple users or sensitive actions are involved.
5. Keep voice output short and avoid reciting sensitive data aloud.

## Masterclass operating standard

When AndreaBot on Alexa is working properly, it should feel like this:

- easy to invoke
- short and natural by voice
- intelligent about when to use OpenClaw/Cursor
- synchronized with Telegram
- secure enough to grow into a real household assistant

That is the bar.

## Related

- [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md)
- [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md)
- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md)
