# Andrea Alexa integration (Custom Skill)

Alexa is a **voice ingress** for the same lockstep bus as Telegram. The skill endpoint in this repo is **`POST /v1/alexa`** on the Andrea sync server ([ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md)).

## Prerequisites

1. **HTTPS endpoint** reachable from Amazon’s skill service. For local-first Mac setups, typical patterns are:
   - **Tailscale** Funnel / HTTPS reverse proxy to `127.0.0.1:ANDREA_SYNC_PORT`, or
   - A small **cloud relay** (API Gateway + Lambda) that forwards JSON to your Mac over a secure tunnel (advanced).

   Alexa Custom Skills expect a documented HTTPS endpoint; self-signed certs are only for Alexa testing workflows—plan for a public hostname + Amazon-trusted cert for household use.

2. **Custom skill** in Alexa Developer Console:
   - **Endpoint**: `https://your-host/v1/alexa`
   - **Interaction model**: create an intent (e.g. `AndreaCaptureIntent`) with a custom slot `utterance` (AMAZON.SearchQuery or a custom type), or reuse the built-in handling in `services/andrea_sync/adapters/alexa.py` which scans slots for the first non-empty `value`.

3. **Account linking** (recommended for multi-user safety): OAuth2 per [Alexa account linking](https://developer.amazon.com/en-US/docs/alexa/account-linking/account-linking-for-custom-skills.html).

4. **Request validation**: For certification, validate Alexa request signatures on your HTTPS endpoint per [security testing](https://developer.amazon.com/en-US/docs/alexa/custom-skills/security-testing-for-an-alexa-skill.html). The reference server in this repo **does not** implement signature verification yet—add it before production certification.

## Behavior

- **LaunchRequest**: short welcome string.
- **IntentRequest** with user text: enqueues `AlexaUtterance` on the lockstep bus (creates task + `UserMessage` event).
- **Stop/Cancel**: polite end session.

Responses are intentionally **short** (voice-safe). Rich detail should be mirrored to Telegram or read from `GET /v1/tasks/{id}`.

## Testing locally

1. Run `python3 scripts/andrea_sync_server.py`.
2. POST a sample Alexa request JSON to `http://127.0.0.1:8765/v1/alexa` (see Alexa request JSON reference in Amazon docs).
3. Confirm `GET /v1/tasks` shows a new task.

## Fire TV / Echo Cube

Devices in the same Amazon account can invoke the skill once published. Use distinct wake-word phrases in the skill description to avoid collisions with other skills.

## Related

- Operations: [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md)
- Comms hygiene: [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md)
