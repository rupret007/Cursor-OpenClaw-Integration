#!/usr/bin/bash
# Run on Linux as an unprivileged user. All downloaded and runtime data are scratch.
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:$PATH"
pilot_source=$(cd -- "$(dirname -- "$0")" && pwd)
[[ $(uname -m) = x86_64 && $(id -u) -ne 0 ]] || {
    echo 'The queue pilot requires an unprivileged x86-64 Linux shell.'
    exit 1
}
node -e 'require("node:sqlite")'
unshare -Urn true
scratch=$(mktemp -d "$HOME/.fo-queue-pilot.XXXXXXXX")
printf 'Private scratch directory: %s\n' "$scratch"
cd "$scratch"

mkdir -p pg hatchet-tools restate-worker
(
    cd pg
    apt download postgresql-18 postgresql-client-18 libpq5 libnuma1 liburing2
    for pkg in *.deb; do dpkg-deb -x "$pkg" .; done
    export LD_LIBRARY_PATH="$PWD/usr/lib/x86_64-linux-gnu" TZ=UTC
    usr/lib/postgresql/18/bin/postgres --version
    usr/lib/postgresql/18/bin/initdb --auth=trust --locale=C -U queue_pilot -D "$PWD/data" >init.log 2>&1
)
(
    cd hatchet-tools
    apt download umoci
    dpkg-deb -x umoci*.deb .
)
python3 -B "$pilot_source/fetch_hatchet.py"
hatchet-tools/usr/bin/umoci unpack --rootless --image hatchet-oci:v0.120.19 hatchet-unpacked

for component in server cli; do
    asset="restate-$component-x86_64-unknown-linux-musl.tar.xz"
    url="https://github.com/restatedev/restate/releases/download/v1.8.1/$asset"
    curl -fsSL --max-time 120 -O "$url" -O "$url.sha256"
    sha256sum -c "$asset.sha256"
    tar -xf "$asset"
done
restate-server-x86_64-unknown-linux-musl/restate-server --version
python3 -m venv hatchet-worker
hatchet-worker/bin/pip -q --disable-pip-version-check install 'hatchet-sdk==2.0.0a6'
(
    cd restate-worker
    npm install --silent --no-fund --no-audit '@restatedev/restate-sdk@1.15.0'
    cp -- "$pilot_source/restate-worker.mjs" .
)

project_wire=()
if [[ -n ${FO_PROJECT_WIRE_RELEASE:-} ]]; then
    : "${FO_PROJECT_WIRE_NODE:?Set the pinned Node executable}"
    : "${FO_PROJECT_WIRE_CLI:?Set the installed OpenClaw entrypoint}"
    project_wire=("FO_PROJECT_WIRE_RELEASE=$FO_PROJECT_WIRE_RELEASE"
                  "FO_PROJECT_WIRE_NODE=$FO_PROJECT_WIRE_NODE" "FO_PROJECT_WIRE_CLI=$FO_PROJECT_WIRE_CLI")
    if [[ -n ${FO_PROJECT_BOT_SOCKET:-} ]]; then
        [[ $FO_PROJECT_BOT_SOCKET = /* && -S $FO_PROJECT_BOT_SOCKET ]] || {
            echo 'The test-bot bridge must be an existing private absolute socket.'
            exit 1
        }
        project_wire+=("FO_PROJECT_BOT_SOCKET=$FO_PROJECT_BOT_SOCKET")
    fi
fi
# The nested namespace maps the loopback namespace owner to a non-root UID for Postgres.
env -i HOME="$HOME" PATH="$PATH" FO_QUEUE_PILOT_HOME="$scratch" "${project_wire[@]}" \
    unshare -Urn bash -c 'ip link set lo up && exec unshare --map-user="$1" --map-group="$2" bash "$3/boot.sh" "$3"' \
    _ "$(id -u)" "$(id -g)" "$pilot_source"
