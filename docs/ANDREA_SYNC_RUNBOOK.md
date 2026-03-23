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
- **Important:** TTL must be **greater than the longest expected job** (OpenClaw/Cursor run). If TTL expires while the first runner is still alive, a **second runner** can start for the same task.
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

## Principal memory / reminders

- Principals, memory notes, preferences, and reminders live in the same SQLite store as the task/event journal.
- Enable `ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED=1` when you want the server to deliver due reminders in the background.
- Tune `ANDREA_SYNC_PROACTIVE_SWEEP_INTERVAL_SECONDS` to control how often due reminders are checked.
- For an on-demand reminder delivery pass, POST `RunProactiveSweep` to `/v1/commands` with the internal bearer token.

## Closed-loop local autonomy

`scripts/andrea_optimize.py` runs one optimization cycle against the local DB and can optionally auto-apply ready proposals via Cursor branch prep.

```bash
cd /path/to/Cursor-OpenClaw-Integration
python3 scripts/andrea_optimize.py \
  --repo . \
  --regression-command "python3 -m unittest discover -p 'test_*.py'" \
  --regression-cwd tests \
  --auto-apply-ready
```

For the operator-grade wrapper, use:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_SYNC_URL='http://127.0.0.1:8765'
export ANDREA_SYNC_INTERNAL_TOKEN='...'
bash scripts/andrea_autonomy_cycle.sh
```

Notes:

- `ANDREA_SELF_HEAL_CURSOR_MODE` overrides the Cursor backend used for branch prep (`auto`, `api`, `cli`).
- `ANDREA_SYNC_CURSOR_REPO` overrides the repo path used by autonomy helpers.
- `scripts/andrea_autonomy_cycle.sh` refuses auto-heal branch prep on a dirty worktree unless `ANDREA_AUTONOMY_ALLOW_DIRTY=1` is set.

## Alexa production

1. Set `ANDREA_ALEXA_SKILL_ID` to the skill’s **applicationId**.
2. Keep `ANDREA_SYNC_ALEXA_EDGE_TOKEN` on the HTTPS edge and the Andrea backend.
3. Set `ANDREA_ALEXA_VERIFY_SIGNATURES=1` on Andrea only when your edge forwards the original Alexa body bytes plus `Signature` and `SignatureCertChainUrl` headers unchanged.
4. If the edge terminates and re-serializes JSON, verify Alexa signatures at the public edge instead of Andrea.
5. Install **`cryptography`** (`pip install cryptography`) wherever `ANDREA_ALEXA_VERIFY_SIGNATURES=1` is enabled.

Intent requests already dedupe via `external_id` = `requestId` on first task creation.

## Optional dependencies

- **cryptography** — required when `ANDREA_ALEXA_VERIFY_SIGNATURES=1`.
