# Install First Officer

Jeff approves activation and runs the activation and rollback commands on bobserver. Start from the verified release currently pinned by the gateway. Keep the bundle and config backup in private storage.

## Stage on the Nuc

From the reviewed PR checkout in a Linux shell, set `PRIOR_BUNDLE` to the private, extracted release bundle. The staging helper checks its inventory against the supported release pins. `python3 -B first-officer/release.py build` runs this sequence and packs the candidate. Use Node 24.16 or newer and Python 3.12 or newer. The private prior bundle is not in this repository, so the gateway tar is not built here.

```bash
set -euo pipefail
umask 077
stage_dir="$(mktemp -d)"
release="$(nice -n 10 python3 -B first-officer/stage.py "$PRIOR_BUNDLE" "$stage_dir")"
candidate="$stage_dir/first-officer-$release"
test_home="$(mktemp -d)"
env HOME="$test_home" FO_CANARY_HOME="$test_home" FO_RELEASE_ROOT="$candidate" \
  nice -n 10 node tests/first_officer/round13-canary.mjs
nice -n 10 node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs
archive="$stage_dir/first-officer-$release.tar"
test ! -e "$archive"
nice -n 10 tar -C "$stage_dir" -cf "$archive" "first-officer-$release"
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

The helper creates a dedicated key on bobserver and adds a restricted authorization on the Mac Mini for one summary command. The Mac reads Andrea locally using the settings already in its deployment. Bobserver receives counts and fixed activity labels.

For an existing restricted key, add `--refresh-reader` to the same command. The helper tests the candidate against recent history and then verifies the refreshed reader from bobserver. Wait for `Andrea reader verified from bobserver.` before publishing or activating the gateway. The gateway keeps running during the reader refresh.

The `@andrea` and LiteLLM routing update can reuse the verified Mac reader. Start with the Bobserver prerequisites below for this gateway-only upgrade.

## Stage the LiteLLM lane policy

Use a private copy of the current LiteLLM YAML and a reviewed JSON policy. The policy has a `fallbacks` object mapping each primary alias to an ordered list of fallback aliases and a `lanes` object keyed by lane name. Each lane specifies `models`, `max_budget` and `budget_duration`. Its model list must include the fallback aliases reachable from its primary aliases. Every routed deployment needs a finite input price and a finite output price, each at least zero and not a boolean, with at least one of them greater than zero. The price may be on `litellm_params` or `model_info`, and the two copies must agree when both are set. Use the existing private environment references for the proxy master key and PostgreSQL connection.

```bash
python3 first-officer/litellm/configure.py "$proxy_config" "$lane_policy" render "$candidate_config"
```

After Jeff approves the proxy switch, use the management environment to provision each lane into its own private credential file. Repeating the command verifies the existing file against the reviewed policy. The management URL is loopback HTTP, or an HTTPS host listed in `FO_LITELLM_HOSTS` that resolves only to private addresses. An empty allowlist refuses every HTTPS proxy URL. Load the Andrea lane key into `ANDREA_LITELLM_API_KEY` in Andrea's service environment, with `ANDREA_LITELLM_BASE_URL` and `ANDREA_LITELLM_MODEL` set to that same reviewed endpoint and alias. `ANDREA_LITELLM_BASE_URL` follows the same loopback-HTTP or allowlisted-private-HTTPS rule, and the lane key is not sent when the endpoint fails it.

```bash
python3 first-officer/litellm/configure.py "$candidate_config" "$lane_policy" provision \
  "$private_proxy_url" "$lane_name" "$lane_credential_file"
```

Use a distinct gateway lane key in `OPENCLAW_LITELLM_API_KEY`. The installer validates a candidate config that references this variable, so make it available to the operator's validation shell and the gateway service through their approved private environment before preparing the switch.

## Track Codex and Claude usage from a lane

Run the tracker before each step and before any long turn. It reads whichever accounts the lane was given and reports both windows for each, with every reset rendered in America/Chicago so CST and CDT are never confused.

Save the Codex reading as the raw `account/rateLimits/read` result from the lane's own `CODEX_HOME`. Save the Claude reading as a JSON object of the response headers from a claude.ai login, which carries `anthropic-ratelimit-unified-status` plus the `5h` and `7d` utilization and reset headers. Pass only the account the lane runs on; a window that is not supplied is reported as unread rather than assumed healthy.

```bash
python3 first-officer/usage/track.py \
  --codex "$codex_reading" --codex-account "$lane_account" --claude "$claude_headers" \
  --repo "$lane_checkout" --step "Step 3" --next "Start step 4" --failure "none"
```

The command exits `0` below the warning threshold, `3` once any window reaches 90 percent, and `4` once any window reaches 97 percent. On either warning it writes `HANDOFF.md` into the lane checkout and commits that one file locally. It never stages another path, never pushes, and refuses to commit on `main` or `master`. It also refuses to write a handoff whose text contains a credential or an address. Use `--warn-percent` and `--stop-percent` only to make the thresholds stricter.

## Queue a review lane

Hatchet is the primary. Restate is the fallback, and only after Hatchet has definitely refused the lane. The dispatcher does not install either service and does not start a worker. Point it at a loopback HTTP listener, or at an HTTPS name listed in `FO_QUEUE_HOSTS` that resolves only to loopback or private addresses. The check happens before a credential is sent. A public address, and any HTTPS name that is not on that list, is rejected. Put each token in the environment variable named by the policy, not in the policy file.

The policy has two objects. `hatchet` carries `base_url`, `tenant`, `workflow` and `token_env`. `restate` carries `base_url`, `service`, `handler` and `token_env`. The ledger path is a private append-only file. The command creates it mode `0600` when it is missing.

```bash
python3 first-officer/lanes/queue.py "$queue_policy" "$lane_ledger" \
  --lane review --head "$head" --pull-request "$number"
```

Exit `0` means the lane is queued. The line is `Lane review is queued on hatchet as <run>.` or `Lane review is queued on restate as <invocation>.` The same command again prints `was already queued` and does not send a second time. Exit `1` means it was not queued. A timeout, HTTP 408, any 5xx, a dropped connection, a redirect, or any answer other than a connection refusal or HTTP 425 or 429 stays unknown and is not failed over. Hatchet and Restate do not share an idempotency check, so an ambiguous Hatchet answer never starts the same lane on Restate. An unknown or pending ledger row blocks a later send until the backend has been checked. A definite refusal from both backends can be retried. The command never pushes, merges or sends a message.

## Send one trace to local Phoenix

The exporter is OpenLLMetry, and the receiver path is Phoenix's OTLP/HTTP endpoint. Give it the collector's base URL, not the `/v1/traces` path. The URL has to be loopback HTTP. A public host, an HTTPS URL, a credential in the URL, or a path is rejected. The attributes file is a JSON object. It must include `fo.lane` and `fo.head`. It may include `fo.backend` (`hatchet`, `restate` or `none`) and `fo.outcome` (`queued`, `refused` or `unknown`). Any other field, including a prompt, is refused before a request is sent. Ambient collector settings and API keys are ignored for the export.

```bash
python3 first-officer/tracing/deliver.py "http://127.0.0.1:6006" "$span_attributes"
```

Exit `0` prints `Phoenix accepted span first-officer.lane for lane <name>.` Exit `1` means the collector did not accept it, or the attributes were refused. The command does not start Phoenix and does not send the span anywhere but the URL you passed.

## Read a Playwright report and apply quarantine

`produce` reads Playwright's JSON report and writes one record per test, including every attempt, plus any top-level report errors. `evaluate` compares that record with a quarantine file and the expected project and test inventory, then writes a summary. Exit `0` means every expected test passed on its first attempt, the report has no top-level error, and the quarantine list is empty of applicable, expired and orphaned entries. Exit `1` means the gate is red. A quarantined test is listed and counted as non-passing even when the test itself passed. A fatal report error or a report that does not match the inventory is red as well. Exit `2` means the report, the quarantine file, or the inventory could not be read.

A quarantine entry names `project`, `file`, `title`, `owner`, `issue`, `reason`, `addedOn` and `expiresOn`. The dates are UTC calendar dates and the window is at most 14 days.

```bash
python3 first-officer/qa/flaky.py produce "$playwright_json" "$flaky_events"
python3 first-officer/qa/flaky.py evaluate "$flaky_events" "$quarantine_json" "$inventory_json" "$summary_json"
```

The inventory file names the expected `projects` and `tests`. A report-level `errors` entry, a missing test, or an unexpected project keeps the gate red. Exit `0` requires the collected tests to match that inventory exactly.

The summary line is `Passing <n>. Non-passing <n>. Quarantined <n>.`

## Publish the shared status file and open the read-only page

`publish` reads an evidence JSON file and writes `STATUS.json` for the fleet reader. The file is mode 0600. Counts are included only when the evidence has them. A node sample older than five minutes is marked missing and stale, and its load numbers are left out. `serve` shows that file on `127.0.0.1` and answers GET only. Stop it with Ctrl-C. It does not collect live load and it does not open a tailnet listener.

```bash
python3 first-officer/status/publish.py publish "$evidence_json" "$status_json"
python3 first-officer/status/publish.py serve "$status_json" --port 8765
```

The publish line is `Published status <timestamp> (<bytes> bytes).` The page line is `Status page on http://127.0.0.1:<port>/`. Exit `1` means the evidence or the file was refused.

## Save one verified fact and dry-run the lane review

`propose` records one plain sentence and prints its nonce and approval digest. The fact is not verified yet. `confirm` saves it only when you pass that nonce and that digest within ten minutes. `recall` prints verified facts. `routine` writes the lane-review dry-run receipt for the next 07:00 America/Chicago and sends nothing. `verify` checks the hash chain against its saved checkpoint, the saved fact and the receipt. Each checkpoint is also written to `.{root}.fo-audit-trust/audit.head` beside the acceptance directory. Deleting the log, the local checkpoint, the key and the receipt does not remove that copy, and `verify` then fails. A fresh directory with no trust copy still verifies. A truncated log, a checkpoint that does not match the last line, or a receipt with no audit line is a failure. Exit `1` means the sentence was refused or the chain did not verify.

```bash
python3 first-officer/acceptance/cycle.py --root "$root" propose "The lane review is weekly."
python3 first-officer/acceptance/cycle.py --root "$root" confirm "$nonce" "$digest"
python3 first-officer/acceptance/cycle.py --root "$root" routine
python3 first-officer/acceptance/cycle.py --root "$root" verify
```

For that sentence the nonce is `mem-b1562a220b03ce99`. The digest changes because it includes the expiry. A verified recall contains `Verified fact: The lane review is weekly.` A second confirm prints `Refused save`. The routine line ends with `Sent no message.`

## Rehearse the authority pilot on a staged lease

`rehearse` writes a private lease under `--root` and runs handoff, one lane, pause and a missed-check-in failover. It refuses the installed runner tree, the conductor state directory and the gateway release store. `checklist` prints the live sequence and does not run it. The live arming commands stay in [CAPTAIN.md](CAPTAIN.md) and wait for Jeff.

```bash
python3 first-officer/pilot/rehearse.py rehearse --root "$root" --epoch 12 --now 1763700000
python3 first-officer/pilot/rehearse.py checklist
```

## Time a staged rollback of the prior pin

The drill writes a private copy, restores the prior config and pin, and prints the elapsed microseconds. It does not stop the gateway. The live rollback command remains `python3 -B first-officer/release.py rollback` and waits for Jeff.

```bash
python3 first-officer/pilot/rollback_drill.py --root "$root"
```

## Scan the checkout for secrets and personal data

The scan reads this checkout, the commits reachable from it, and any `*.log` file or `logs` directory under it, including trees named `data` or `artifacts`. A file or directory symlink is a failure and is not followed. `.git`, `__pycache__`, `.pytest_cache`, `node_modules`, `.venv` and `venv` are not scanned. The local runtime files `data/andrea_sync.db`, `data/andrea_sync.db-wal` and `data/andrea_sync.db-shm` are not reported when they are binary; readable text at those paths is scanned, and any other file under `data/` or `artifacts/` is scanned. Exit 0 prints one clean line. Exit 1 names the rule and the location and does not print the value. A sensitive name in a commit path is a finding too, including both sides of a diff and a rename or copy, and the report shows that path as `[redacted]`. A shallow clone is refused. The offline gate runs this command before the unit tests.

```bash
python3 first-officer/privacy/scan.py
```

The clean line is `Secret scan clean. Files <n>. History commits <n>. Logs 0.` The counts move when the branch moves. A line that names `credential`, `phone`, `address`, `email`, or `private-key` is a failure.

## Fetch one allowlisted page

`first-officer/guard/restricted_fetch.py` reads a page only when the host is an exact allowlist entry. Public entries are HTTPS on port 443 and must resolve to public unicast addresses. `http://127.0.0.1:<port>` is accepted only as a named fixture. One redirect is checked with the same rules. The visible text is returned inside a random fence from `first-officer/guard/sanitize.py`. A fence closer, a hidden character, or a page over 8 KiB after scripts are removed is refused, and the body is not returned. Fenced text is not a tool call. The gateway's existing `before_tool_call` block is unchanged. Recalled preference, episodic, and principal memory lines are fenced the same way, at 500 characters. A line that cannot be fenced is omitted. The direct reply keeps only those fenced lines.

Live news, grounded research, recent-message lookup, and personal-assistant synthesis go through `first-officer/guard/boundary.py`. Each of those routes has its own tool allowlist: `fetch` for the two page lookups, `read_messages` for recent messages, and no tools for the synthesis route. None of those names is an outbound skill. The lookup does not launch the unrestricted OpenClaw agent. A normal news or research question does not need a URL in the prompt. The allowlist may name `sources` for `structured_live_news` and `structured_grounded_research`, and those exact pages are the only ones fetched for the query. A URL inside the fetched page is not followed. Recent messages are read from the local JSON file in `FO_MESSAGE_SOURCE` and are not requested from a messaging service. Synthesis uses only the assistant-state lines already in the prompt. A page or message is returned inside the fence, and `visible_to_tools` is false for that fence, so the text cannot select `send_message`, `bluebubbles`, or another outbound tool. If the allowlist has no source for a page lookup, `FO_MESSAGE_SOURCE` is unset, or the synthesis prompt has no assistant-state lines, the route tells the user it is off instead of returning a silent refusal. Set `FO_FETCH_ALLOWLIST` to the reviewed allowlist file. An empty allowlist refuses every fetch. The exact send phrases remain `send it`, `send it now`, and `send now`. A draft is bound to a digest of the action, target, exact body, principal, origin chat, and expiry. A confirmation in another chat for the same principal does not consume it. Sending clears the draft only after a tool receipt matches that digest. The server installs that delivery tool itself. An exact confirmation writes one private outbox record and returns the receipt read back from that record. A second confirmation does not write another record. A model `ok` status is not delivery.

## Pack the review archive

`pack` writes a checksummed tar of this lane outside the checkout. `verify` reads that tar and checks every file against `SHA256SUMS`. The archive is mode 0600. It does not read the private prior bundle and it does not call the gateway. A file the secret scan rejects stops the pack and leaves no archive.

```bash
rm -rf /tmp/fo-review
python3 first-officer/archive.py pack /tmp/fo-review
archive="$(printf '%s\n' /tmp/fo-review/first-officer-review-*.tar)"
python3 first-officer/archive.py verify "$archive"
```

Pack prints `Review archive <digest>`, the tar path, and `The gateway was not touched.` Verify prints `Review archive <digest> verified. The gateway was not touched.` The two digests match. This tar is the offline copy. Gateway activation still uses the private prior bundle and `first-officer/release.py`.

## Check prerequisites on bobserver

Run from the PR checkout as the account that owns `openclaw-gateway.service`. The GitHub CLI must authenticate as the configured owner account. The existing Andrea read-only bridge must answer from the service account using its private reader configuration.

```bash
set -euo pipefail
umask 077
systemctl --user is-active --quiet openclaw-gateway.service
test "$(gh api user --hostname github.com --jq .login 2>/dev/null)" = rupret007
test -x /usr/bin/gh
baseline=61ff4565dd5638a886effefbd720c92a17b7388acbc6eeaffb7779f78d3f1b36
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

If the Andrea check fails, resolve it while the current gateway is running. For a Mac-hosted Andrea, use the restricted SSH setup above. Existing authenticated loopback readers can continue using their private `~/.config/openclaw-firstofficer/andrea-reader.json`. Keep the Andrea control credential on its current host and use the Mac-hosted setup for new connections. Set `baseline` to the verified active release on hosts running an earlier pinned bundle.

## Publish and activate the reviewed candidate

`python3 -B first-officer/release.py activate` is the single activation command. It runs the sequence below, stops at the first failure, and records one private rollback. `--dry-run` prints the commands and runs none of them. The service pin continues to use the root-owned verifier and its existing fallback slot. The private backup records the exact prior config and pin for rollback.

```bash
archive="$HOME/.openclaw/segment1-staged/first-officer-$release.tar"
extract_dir="$(mktemp -d)"
tar -xf "$archive" -C "$extract_dir" --no-same-owner
candidate="$extract_dir/first-officer-$release"
python3 -B -c 'import hashlib, pathlib, sys; digest = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(); sys.exit(0 if digest == sys.argv[2] else 1)' "$candidate/SHA256SUMS" "$release"
(cd "$candidate" && sha256sum --quiet --strict --check SHA256SUMS >/dev/null 2>&1)
backup_parent="$(mktemp -d "$HOME/.openclaw/backups/fo-segment1.XXXXXXXX")"
backup="$backup_parent/switch"
nice -n 10 python3 -B first-officer/install.py prepare "$backup" --release "$release" --from-release "$baseline" --candidate "$candidate"
printf 'Keep this private rollback directory: %s\n' "$backup"
published="/opt/openclaw-fo/first-officer-$release"
sudo test ! -e "$published"
publish_dir="$(sudo mktemp -d /opt/openclaw-fo/.segment1-publish.XXXXXXXX)"
sudo cp -a --no-preserve=ownership "$candidate/." "$publish_dir/"
sudo chmod -R a-w,a+rX "$publish_dir"
sudo mv -nT "$publish_dir" "$published"
nice -n 10 python3 -B first-officer/install.py activate "$backup"
systemctl --user is-active --quiet openclaw-gateway.service
```

The manifest digest is compared with the approved release, and the private rollback directory is written, before the candidate is published. Preparation passes the unpacked candidate directory to the existing verifier, because the published release path does not exist yet, and also validates both configs with the OpenClaw launcher used by the running service. Rollback restores the prior bundle's config and pin; the prior bundle files stay in place. With a root-owned pin, activation obtains sudo authorization before shutdown and preserves the pin’s owner, group and mode. Activation waits up to 120 seconds for local readiness and attempts to restore the prior config and pin if the switch fails. The rollback restores that config, pin, and service. It does not delete the published candidate directory or undo a separately provisioned reader or proxy. Run [the Telegram acceptance checks](LIVE-TEST.md) once the service is ready.

## Roll back to the exact prior config and pin

Use the backup directory printed by the activation procedure. The rollback restores both files, reloads the user service manager, starts the gateway and waits for the readiness endpoint.

```bash
set -euo pipefail
read -r -p 'Enter the private rollback directory: ' backup
nice -n 10 python3 -B first-officer/install.py rollback "$backup"
systemctl --user is-active --quiet openclaw-gateway.service
```

If automatic recovery itself fails, use the config and pin in that private backup with the existing Round 13 recovery procedure. Keep those files private and preserve them until the restored service has passed its readiness check.

If the Mac reader was refreshed for this upgrade, restore the prior restricted command using [the Captain rollback steps](CAPTAIN.md#take-back-control-and-roll-back).

To revoke the dedicated Mac reader as well, remove only the key line labeled `openclaw-firstofficer-andrea-read` from the Mac account’s `~/.ssh/authorized_keys`. This revokes First Officer’s reader access while preserving other Mac SSH logins.
