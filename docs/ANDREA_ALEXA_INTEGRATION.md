# Andrea Alexa integration (Custom Skill)

Alexa is an **explicit voice doorway** into the same Andrea/OpenClaw/Cursor lockstep system as Telegram. The skill endpoint in this repo is **`POST /v1/alexa`** on the Andrea sync server, but the recommended real deployment shape is:

`Alexa Custom Skill -> thin cloud edge -> local andrea_sync`

See [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md) for the forwarding contract.
The repo also includes a reference edge implementation at [scripts/alexa_edge_lambda.py](../scripts/alexa_edge_lambda.py).

If you want the user-facing, click-by-click setup path, start with [ANDREA_ALEXA_USER_SETUP.md](ANDREA_ALEXA_USER_SETUP.md).

## UX rules

- Invocation stays explicit: `Alexa, ask Andrea Bot ...`
- Alexa answers briefly and in plain spoken language.
- Delegated or long-running work should not dump technical detail into voice.
- Telegram receives exactly one compact summary per Alexa task/session when the work finishes.
- Andrea stays the narrator even when OpenClaw or Cursor do the heavy lifting behind the scenes.

Canonical example:

- skill display name: `AndreaBot`
- invocation name in Alexa Developer Console: `andrea bot`
- spoken phrase: `Alexa, ask Andrea Bot how are you today`

## Current runtime behavior

- **LaunchRequest**: short welcome string and keep the session open.
- **IntentRequest** with user text:
  - creates an `AlexaUtterance` task in lockstep
  - stores Alexa session metadata (`session_id`, `request_id`, `intent_name`, `locale`, `user_id`, `device_id`) in task projection metadata
  - routes the request through Andrea-first logic
  - returns either:
    - a direct short spoken Andrea reply, or
    - a short acknowledgement when the request is delegated to OpenClaw/Cursor
- **Stop/Cancel**: polite end session.

## Routing model

Alexa uses the same routing brain as Telegram, but with voice-specific output rules:

- lightweight personal-assistant turns can be answered directly by Andrea
- heavier assistant skill, repo, debugging, or execution tasks are delegated into the `openclaw_hybrid` lane
- if Cursor becomes necessary, the delegated task escalates there through the existing OpenClaw/Cursor coordination path
- Alexa itself does not narrate every `queued/running/progress` transition

## Telegram summary behavior

When an Alexa task reaches `completed` or `failed`, Andrea can send one compact summary message to Telegram.

Relevant env vars:

- `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM=1` (enabled by default; set `0` to disable)
- `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID=<chat id>`
- fallback: `TELEGRAM_CHAT_ID`
- `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED=1` to allow delegated Alexa/OpenClaw/Cursor work globally
- `ANDREA_SYNC_TELEGRAM_AUTO_CURSOR=1` only controls Telegram auto-execution, not Alexa

## Recommended deployment

### 1. Alexa Developer Console

- Create a **Custom Skill**
- Endpoint points to your public edge, not directly to the home machine when possible
- Define an intent such as `AndreaCaptureIntent`
- Use a free-form slot such as `utterance` (`AMAZON.SearchQuery` works well)

### 2. Cloud edge

The edge should:

- verify Alexa request signatures
- optionally handle account linking / user identity mapping
- forward the raw Alexa JSON body to the local Andrea endpoint
- attach `Authorization: Bearer $ANDREA_SYNC_ALEXA_EDGE_TOKEN`

The reference edge helper in this repo is intentionally a forwarding example; it does not replace the production Alexa signature/certificate verification step.

The local Andrea server now supports this optional token and rejects unauthorized forwarded requests when `ANDREA_SYNC_ALEXA_EDGE_TOKEN` is set.

### 3. Local Andrea backend

- Run `python3 scripts/andrea_sync_server.py`
- Keep Telegram configured if you want Alexa session summaries mirrored there
- Keep the OpenClaw/Cursor stack running so delegated Alexa requests can complete end-to-end
- If `ANDREA_SYNC_ALEXA_EDGE_TOKEN` is set, local/manual `curl` requests must include the bearer token too

## Security and certification

For production/certification, treat these as required:

1. Validate Alexa signatures at the cloud edge.
2. Use TLS on the public Alexa endpoint.
3. Set `ANDREA_SYNC_ALEXA_EDGE_TOKEN` on the local backend and have the edge forward it.
4. Use account linking or equivalent identity mapping for multi-device / multi-user safety.

The local `/v1/alexa` endpoint is intentionally small and voice-focused; certification-specific verification is expected to live at the edge.

## Testing locally

1. Run `python3 scripts/andrea_sync_server.py`.
2. POST a sample Alexa request JSON to `http://127.0.0.1:8765/v1/alexa`.
3. Confirm:
   - direct conversational turns return a spoken reply immediately
   - heavier tasks return a short acknowledgement
   - `GET /v1/tasks` shows an Alexa task
   - the task projection contains `meta.alexa`

Example:

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

If `ANDREA_SYNC_ALEXA_EDGE_TOKEN` is unset, you can omit the `Authorization` header for local testing.

## Device rollout

Recommended validation order:

1. Alexa iPhone app first
2. Fire TV Cube / Fire Stick once the voice path feels stable

Use brief phrases and confirm that the Telegram mirror stays to one summary message per Alexa task.

## Related

- Architecture: [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md)
- Operations: [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md)
- Cloud edge: [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md)
