#!/usr/bin/bash
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:/usr/bin:/bin"
export TZ=UTC
cd "$FO_QUEUE_PILOT_HOME"
[[ $(ip -o link show | cut -d: -f2 | xargs) = lo ]] || {
    echo "Run the queue pilot inside its private loopback-only namespace."
    exit 1
}
mkdir -p logs pg/sock hatchet-config
run=$(mktemp -d "$PWD/run.XXXXXXXX")
export PILOT_EFFECTS="$run/effects.sqlite" PILOT_WORKER_PORT=59080 PILOT_LEDGER="$run/lanes.jsonl"
printf 'shutdown-timeout = "2s"\n[worker]\ncleanup-interval = "1s"\nrule-book-poll-interval = "1s"\n' >"$run/restate.toml"
export DATABASE_URL="postgresql://queue_pilot@localhost:55490/postgres?sslmode=disable"
export SERVER_PORT=58888 SERVER_HEALTHCHECK_PORT=58733 SERVER_GRPC_PORT=57070
export SERVER_GRPC_BROADCAST_ADDRESS=localhost:57070 SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS=localhost:57070
export SERVER_GRPC_BIND_ADDRESS=localhost SERVER_GRPC_INSECURE=t SERVER_URL=http://localhost:58888
export SERVER_AUTH_COOKIE_DOMAIN=localhost SERVER_AUTH_COOKIE_INSECURE=t SERVER_AUTH_SET_EMAIL_VERIFIED=t
hatchet="$PWD/hatchet-unpacked/rootfs"
export LITE_FRONTEND_PORT=58889 LITE_RUNTIME_PORT=58882 LITE_STATIC_ASSET_DIR="$hatchet/static-assets"
server="$PWD/restate-server-x86_64-unknown-linux-musl/restate-server"
children=()
cleanup() {
  if (("${#children[@]}")); then
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
poll() {
    for _ in $(seq 1 70); do
        if curl --max-time 1 -fsS "$1" >/dev/null 2>&1; then return; fi
        sleep .2
    done
    printf 'Listener unavailable (%s)\n' "$2"
    return 1
}
start() {
    local log=$1
    shift
    nice -n 10 "$@" >"logs/$log" 2>&1 &
    children+=("$!")
}
start postgres env LD_LIBRARY_PATH="$PWD/pg/usr/lib/x86_64-linux-gnu" "$PWD/pg/usr/lib/postgresql/18/bin/postgres" -D "$PWD/pg/data" -k "$PWD/pg/sock" -h localhost -p 55490 -c timezone=UTC
for _ in $(seq 1 70); do
    if env LD_LIBRARY_PATH="$PWD/pg/usr/lib/x86_64-linux-gnu" "$PWD/pg/usr/lib/postgresql/18/bin/pg_isready" -h localhost -p 55490 >/dev/null 2>&1; then break; fi
    sleep .2
done
nice -n 10 "$hatchet/hatchet-migrate" >logs/migrate 2>&1 || { echo 'Hatchet migration failed'; exit 1; }
nice -n 10 "$hatchet/hatchet-admin" quickstart --skip certs --generated-config-dir "$PWD/hatchet-config" --overwrite=false >logs/quickstart 2>&1 || { echo 'Hatchet quickstart failed'; exit 1; }
export PILOT_HATCHET_BIN="$hatchet"
loopback=$(python3 -c 'import ipaddress; print(ipaddress.IPv4Address(2130706433))')
start restate env RESTATE_DISABLE_TELEMETRY=true RESTATE_ROCKSDB_TOTAL_MEMORY_SIZE=128MiB "$server" -c "$run/restate.toml" --base-dir "$run/datastore" --node-name queue-pilot --default-num-partitions 1 --bind-ip "$loopback" --listen-mode tcp --no-logo
poll "http://localhost:9070/health" 'Restate'
nice -n 10 python3 -B "$1/pilot.py"
