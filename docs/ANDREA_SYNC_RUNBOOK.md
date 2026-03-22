# Andrea sync operator runbook

## Kill switch

Engagement is the **OR** of:

1. Environment `ANDREA_SYNC_KILL_SWITCH=1`
2. Touch file `ANDREA_SYNC_DB` path + `.kill` (same stem as the SQLite file)
3. SQLite `meta` key written by `KillSwitchEngage` command

**Releasing** via `KillSwitchRelease` clears the DB flag only. You must also clear the env var and delete the `.kill` file if those are set.

## Stuck delegated execution (`executor_started`)

When `ANDREA_SYNC_BACKGROUND_ENABLED=1`, the server sets meta `andrea_bridge:executor_started:{task_id}` before spawning the OpenClaw/Cursor thread. If the process crashes after that marker is written, **no second runner** starts for that task until the marker is cleared.

**Mitigation:**

- Set `ANDREA_SYNC_EXECUTOR_STARTED_TTL_SECONDS` to a positive value (e.g. `14400` for 4h) to allow reclaim after the TTL.
- Or delete the meta key manually (SQLite `DELETE FROM meta WHERE key LIKE 'andrea_bridge:executor_started:%'`).

## Schema / migrations

`store.migrate()` is **create-if-not-exists** only. Bumping behavior requires explicit migration scripts and updating `meta.schema_version` in a controlled way. Prefer additive columns/tables with `IF NOT EXISTS` and one-off upgrade functions.

## JSON event payloads

If `ANDREA_SYNC_JSON_PARSE_WARNINGS=1`, corrupt `payload_json` rows log a one-line warning with `task_id` and `seq` during projection load.

## Health endpoint privacy

By default `/v1/health` returns only the **basename** of the DB file. Set `ANDREA_SYNC_HEALTH_VERBOSE=1` for the full path (useful on localhost debugging).

## Observability toggles

| Env | Effect |
|-----|--------|
| `ANDREA_SYNC_STRUCTURED_LOG=1` | One JSON line per structured event (e.g. Alexa verify failures) |
| `ANDREA_SYNC_METRICS_LOG=1` | One JSON line per `metric_log` counter |

## Alexa production

1. Set `ANDREA_ALEXA_SKILL_ID` to the skill’s **applicationId**.
2. Set `ANDREA_ALEXA_VERIFY_SIGNATURES=1`.
3. Install **`cryptography`** (`pip install cryptography`).
4. Keep `ANDREA_SYNC_ALEXA_EDGE_TOKEN` on the HTTPS edge if you use a shared URL.

Intent requests already dedupe via `external_id` = `requestId` on first task creation.

## Optional dependencies

- **cryptography** — required when `ANDREA_ALEXA_VERIFY_SIGNATURES=1`.
