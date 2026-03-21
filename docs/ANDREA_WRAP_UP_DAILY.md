# Daily wrap-up (operator)

Use this sequence at end of day or before a release.

## 1. Live prerequisites (optional but recommended)

```bash
cd /Users/andreabot/repos/Cursor-OpenClaw-Integration
export ANDREA_SYNC_INTERNAL_TOKEN='...'
export ANDREA_SYNC_URL='http://127.0.0.1:8765'   # default if unset
bash scripts/andrea_wrap_up_prereqs.sh
```

Before this step, make sure `andrea_sync` is already running, for example:

```bash
cd /Users/andreabot/repos/Cursor-OpenClaw-Integration
export ANDREA_SYNC_INTERNAL_TOKEN='...'
export ANDREA_SYNC_TELEGRAM_SECRET='...'   # if you use Telegram ingest
python3 scripts/andrea_sync_server.py
```

Exits `0` when the token is set and `/v1/health` responds. Warns if Telegram vars are missing (optional).

## 2. Full operator cycle (live)

Requires `andrea_sync` running and `ANDREA_SYNC_INTERNAL_TOKEN` set.

```bash
bash scripts/andrea_full_cycle.sh
```

Notes:
- The script already runs `git pull --ff-only origin main`. If you are not on a clean `main` checkout, use `SKIP_GIT=1 bash scripts/andrea_full_cycle.sh`.
- If the cycle aborts during the kill-switch drill, recover with `bash scripts/andrea_kill_switch.sh release`.

Optional Telegram-aware run:

```bash
export TELEGRAM_BOT_TOKEN='...'
export ANDREA_SYNC_TELEGRAM_SECRET='...'
export ANDREA_FULL_CYCLE_WAIT_TELEGRAM=1   # optional
bash scripts/andrea_full_cycle.sh
```

## 3. Offline / CI safety net

Always safe to run; does not require a running sync server (unless you set `RUN_COMM_SMOKE=1`).

```bash
bash scripts/test_integration.sh
```

## Minimum recurring commands

| When | Command |
|------|---------|
| Before live cycle | `bash scripts/andrea_wrap_up_prereqs.sh` |
| Full check | `bash scripts/andrea_full_cycle.sh` |
| Offline gate | `bash scripts/test_integration.sh` |

See also [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md).
