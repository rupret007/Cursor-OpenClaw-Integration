# Install First Officer Segment 1

Jeff runs the activation and rollback commands on bobserver. The candidate starts from the Round 13 release pinned by the gateway. Keep the bundle and config backup in private storage.

## Stage on the Nuc

From the reviewed PR checkout in a Linux shell, set `ROUND13_BUNDLE` to the private Round 13 bundle directory from the one-time snapshot. It must contain `SHA256SUMS`. Use Node 24.16 or newer and Python 3.12 or newer.

```bash
set -euo pipefail
umask 077
stage_dir="$(mktemp -d)"
release="$(nice -n 10 python3 -B first-officer/stage.py "$ROUND13_BUNDLE" "$stage_dir")"
candidate="$stage_dir/first-officer-$release"
test_home="$(mktemp -d)"
env HOME="$test_home" FO_CANARY_HOME="$test_home" FO_RELEASE_ROOT="$candidate" \
  nice -n 10 node tests/first_officer/round13-canary.mjs
nice -n 10 node --test tests/first_officer/segment1.test.mjs
nice -n 10 tar -C "$stage_dir" -cf "$stage_dir/first-officer-$release.tar" "first-officer-$release"
printf 'Release %s\nPrivate bundle %s\n' "$release" "$stage_dir/first-officer-$release.tar"
```

Pass the digest and the tar file to Jeff through a private channel. Put the tar file under `~/.openclaw/segment1-staged/` on bobserver and use the same PR checkout for the operator commands.

## Connect Andrea on the Mac Mini

If this is the first connection to Andrea from bobserver, run the provisioning helper from the Nuc. It uses Jeff’s existing SSH logins to both machines. Check that a normal SSH login to each already works from this terminal. Enter the Mac Mini SSH host and account when prompted.

```bash
read -r -p 'Mac Mini SSH host: ' mac_host
read -r -p 'Mac Mini username: ' mac_user
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --bob-host bobserver --mac-host "$mac_host" --mac-user "$mac_user"
```

The helper creates a dedicated key on bobserver and adds a restricted authorization on the Mac Mini for one summary command. The Mac reads Andrea locally using the settings already in its deployment. Bobserver receives counts and detected themes. Wait for `Andrea reader verified from bobserver.` before publishing or activating the gateway. The helper verifies an actual summary and leaves the gateway running.

## Check prerequisites on bobserver

Run from the PR checkout as the account that owns `openclaw-gateway.service`. The GitHub CLI must authenticate as the configured owner account. The existing Andrea read-only bridge must answer from the service account using its private reader configuration.

```bash
set -euo pipefail
umask 077
systemctl --user is-active --quiet openclaw-gateway.service
test "$(gh api user --hostname github.com --jq .login 2>/dev/null)" = rupret007
test -x /usr/bin/gh
baseline=daf8df40c3b03440d69db6b61a271362b4b442551c7d3648b0b87c6f8b046ecf
if ! node --input-type=module - "/opt/openclaw-fo/first-officer-$baseline" >/dev/null 2>&1 <<'JS'
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { sshAndreaSummary } from './first-officer/segment1/andrea-ssh.mjs';
if (await sshAndreaSummary(AbortSignal.timeout(15000))) process.exit(0);
const { createAndreaReader } = await import(pathToFileURL(join(process.argv[2], 'round6/runtime/andrea.mjs')));
await createAndreaReader().chats();
JS
then
  printf '%s\n' 'Andrea read-only preflight failed. Check the existing reader and its private configuration.' >&2
  exit 1
fi
read -r -p 'Enter the reviewed release digest: ' release
[[ "$release" =~ ^[a-f0-9]{64}$ && "$release" != "$baseline" ]]
```

If the Andrea check fails, resolve it while the current gateway is running. For a Mac-hosted Andrea, use the restricted SSH setup above. Existing authenticated loopback readers can continue using their private `~/.config/openclaw-firstofficer/andrea-reader.json`. Keep the Andrea control credential on its current host and use the Mac-hosted setup for new connections.

## Publish and activate the reviewed candidate

The service pin continues to use the root-owned verifier and its existing fallback slot. The private backup records the exact prior config and pin so the operator can return to Round 13.

```bash
archive="$HOME/.openclaw/segment1-staged/first-officer-$release.tar"
extract_dir="$(mktemp -d)"
tar -xf "$archive" -C "$extract_dir" --no-same-owner
candidate="$extract_dir/first-officer-$release"
test "$(sha256sum "$candidate/SHA256SUMS" | cut -d' ' -f1)" = "$release"
(cd "$candidate" && sha256sum --quiet --strict --check SHA256SUMS >/dev/null 2>&1)
published="/opt/openclaw-fo/first-officer-$release"
sudo test ! -e "$published"
publish_dir="$(sudo mktemp -d /opt/openclaw-fo/.segment1-publish.XXXXXXXX)"
sudo cp -a --no-preserve=ownership "$candidate/." "$publish_dir/"
sudo chmod -R a-w,a+rX "$publish_dir"
sudo mv -nT "$publish_dir" "$published"

backup_parent="$(mktemp -d "$HOME/.openclaw/backups/fo-segment1.XXXXXXXX")"
backup="$backup_parent/switch"
nice -n 10 python3 -B first-officer/install.py prepare "$backup" --release "$release"
printf 'Keep this private rollback directory: %s\n' "$backup"
nice -n 10 python3 -B first-officer/install.py activate "$backup"
systemctl --user is-active --quiet openclaw-gateway.service
```

Preparation validates the candidate with the existing verifier and OpenClaw before the service is stopped. Activation waits up to 120 seconds for local readiness and attempts to restore the prior config and pin if the switch fails. Run [the Telegram acceptance checks](LIVE-TEST.md) once the service is ready.

## Roll back to the exact prior config and pin

Use the backup directory printed by the activation procedure. The rollback restores both files, reloads the user service manager, starts the gateway and waits for the readiness endpoint.

```bash
set -euo pipefail
read -r -p 'Enter the private rollback directory: ' backup
nice -n 10 python3 -B first-officer/install.py rollback "$backup"
systemctl --user is-active --quiet openclaw-gateway.service
```

If automatic recovery itself fails, use the config and pin in that private backup with the existing Round 13 recovery procedure. Keep those files private and preserve them until the restored service has passed its readiness check.

To revoke the dedicated Mac reader as well, remove only the key line labeled `openclaw-firstofficer-andrea-read` from the Mac account’s `~/.ssh/authorized_keys`. This revokes First Officer’s reader access while preserving other Mac SSH logins.
