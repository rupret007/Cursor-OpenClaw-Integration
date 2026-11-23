# OpenClaw captain on Telegram

OpenClaw can captain exact-head CI reviews for open PRs in Cursor-OpenClaw-Integration. The pilot scope is `integration-ci`. Each review uses one isolated lane on the existing subscription runner. OpenClaw checks the live fleet, gate, runner quota and required GitHub checks before dispatch. It follows a pending CI head for up to 20 minutes and verifies the current head again after the worker exits.

The staged pilot and the staged pin restore are proven on a private copy. They do not read the live lease and they do not stop the gateway. Production authority stays with the current holder until the live checklist on this page is run. Gateway activation is one command, `python3 -B first-officer/release.py`, with one rollback. `python3 first-officer/archive.py pack` writes a checksummed copy of this lane for offline testing. That copy is not the gateway release.

## Independent Mac Mini and Nuc profiles

The Mac Mini and Nuc are the standalone Captain targets. Each needs its own gateway, Captain state, queue and LiteLLM route. Andrea runs on the Mac Mini. Bobserver is an optional fleet member and an opt-in compute worker. Run the full acceptance suite on each host with Bobserver unreachable. Use a separate test Telegram account for the rehearsal and a no-cost model route or loopback provider fixtures.

Set `FO_LOCAL_NODE=mac-mini` for the Mac gateway or `FO_LOCAL_NODE=nucbox` for the Nuc gateway. Local load uses that host's label. Local load and tracked process counts come from the active host. Standalone admission checks live local capacity and the local gate. Remote fleet samples still need fresh timestamps. On the Mac, the gateway reads Andrea locally under the Andrea LaunchAgent account. On the Nuc, it connects to the same read-only Mac reader over a dedicated SSH route.

The independent reader route uses a dedicated Nuc key and a pinned Mac host key. After approval to add that key to the Mac, run from the Nuc checkout under the same Linux account that will run the gateway:

```bash
read -r -p 'Mac Mini SSH host: ' mac_host
read -r -p 'Mac Mini username: ' mac_user
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --local-client --mac-host "$mac_host" --mac-user "$mac_user"
```

The local provisioning path connects only to the Mac. The Mac has separate restricted entries for the Nuc and the incumbent reader. The Nuc key can run one read-only summary command. Refreshes and restores on the Nuc use the same `--local-client` flag.

### Standalone authority rehearsal

The supported standalone launcher checks the local lease and holds an exclusive lock for the lifetime of the gateway process. A transfer requires the releasing gateway to exit and its reviews to settle. The source parks before issuing a signed handoff. The recipient saves the existing work and replay history before accepting a newer epoch. Jeff grants the review scope again at that epoch. The same export can be recovered after an interrupted copy. A parked host refuses gateway startup. Rollback transfers authority in the reverse direction at a newer epoch.

Rehearse with the [authority tests](tests/first_officer/test_captain.py). They use separate scratch directories and real OpenSSH Ed25519 signatures. The production launcher and operator commands below use the installed candidate, and require the approved two-host setup. Keep both host keys private and exchange only their verified public keys, saved as `peer.pub` with mode `0600`. Place each host's own key at `~/.local/state/openclaw-captain/handoff-key` inside a mode `0700` directory. After confirming the old production Telegram consumer has stopped and Jeff approves the transfer, initialize only the Mac Mini.

```bash
# Run from the Mac Mini under the intended gateway account.
export FO_LOCAL_NODE=mac-mini
printf '{}\n' | python3 -I -B "$captain_state" local-initialize

# The launchd or systemd service runs this command under its local account.
python3 -I -B "$captain_state" local-run "$openclaw_bin"
```

Here `captain_state` is the candidate's `round6/runtime/segment1/captain-state.py` and `openclaw_bin` is the absolute path of the installed OpenClaw executable named `openclaw`. The service environment selects its own `FO_LOCAL_NODE`. Never launch the same bot account outside the guarded service. A lost or restored authority directory needs operator reconciliation with both hosts, because an offline peer cannot detect that the other host restored old state.

For a planned transfer, pause and drain the review scope, stop the current gateway service, and obtain its exact epoch from the local authority view. Save the handoff as a private file and copy it to the paired host over a trusted connection. Keep it out of logs and source control.

```bash
umask 077
printf '{}\n' | python3 -I -B "$captain_state" view
printf '{"epoch":%s}\n' "$captain_epoch" |
  python3 -I -B "$captain_state" local-release > "$private_handoff"

# Run under the destination account with its own FO_LOCAL_NODE.
python3 -I -B "$captain_state" local-import < "$private_handoff"
```

### Unattended Mac standby design

The Nuc is primary. The Mac Mini can take over after **90 consecutive seconds without a valid Nuc renewal**. Renew every 15 seconds. Measure expiry on the authority service's clock. A standby with verified dependencies should answer a test command within two minutes of the Nuc going down. The Mac stays Captain when the Nuc returns. Handback uses the planned transfer.

Before enabling automatic promotion, establish an independent authority and side-effect fence on an always-on device. Check whether the existing NAS can host it independently of either Captain host. Bobserver is optional compute. The fence must handle these conditions.

- Grant one active host a monotonically increasing epoch. A missed renewal makes the Mac eligible to ask for authority. The Mac can act only after the fence issues a new epoch. If the fence is unreachable, stop admitting new side effects.
- Put the production Telegram token and polling cursor behind one relay. Deliver updates only to the current holder. Check its epoch before forwarding replies and preserve uncertain send receipts. Remove direct Telegram access from both gateway instances before enabling promotion.
- Require the current epoch for new Hatchet or Restate submissions at the queue ingress. Reconcile existing runs and delivery receipts by their original IDs. Preserve pending work for review and do not automatically retry submissions or replies with uncertain outcomes. Coding grants remain bound to their original epoch.
- Recover the required Captain state from an independent durable copy. If that copy is missing or inconsistent, suspend the affected routes and report the blocker. A witness restart must preserve its last issued epoch and wait a full renewal window before reassigning an ambiguous lease to the Mac. This conservative recovery can move Captain to the Mac after a witness restart.

The standalone generator accepts `--mac-launch-daemon` to render a system service under the unprivileged gateway account. The current local-authority guard still parks the Mac without an approved handoff. Run the generator as the Mac gateway user. Installation in the system launchd domain is a one-time administrator task. A system service can run at the login window after macOS has started. A Mac waiting at FileVault's preboot unlock screen cannot provide standby. Verify Andrea separately because it uses a user-session dependency. [Apple launchd guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)

The offline lease store in `first-officer/standby.py` tests the deadline, stale-epoch rejection, and cross-epoch effect reservations. Its database belongs on the broker's local disk. A mounted archive share alone supplies neither a broker process nor a fence. The loopback-tested broker in `first-officer/standby_broker.py` uses mutual TLS and pins one client certificate to each host. The client checks the broker certificate and host name. The broker derives the node identity from the certificate and denies effects until an adapter is registered. An optional private Telegram configuration enables the broker to own long polling for one verified bot and owner. The broker stores selected text updates in its private inbox before confirming them upstream and clears their text when a final reply receipt settles. Restart begins without a saved upstream offset and deduplicates any replay by message ID. An unconfirmed final reply leaves a receipt for review without blocking the next inbox entry. An optional `--review-queue-config` uses the same configured Hatchet and Restate review routes and exact-result verifier as standalone Captain. Keep their submission credentials on the broker. A private `review-scope.json` in its state directory grants one exact `request` at an `epoch` until `expires` (Unix seconds, no more than one hour ahead). Its request contains the lane, PR number and full head. The broker rechecks the lease and scope before sending to either backend. The active host can read an original review receipt and verify the accepted run after an epoch change. The optional `--project-queue-config` selects the dedicated Hatchet coding workflow. The separate private `project-scope.json` binds one exact coding request, Nuc lease epoch, `worker: local-qwen` and expiry. The broker holds at most one unresolved or accepted request per repository. A confirmed refusal releases the reservation. After promotion, the Mac can report the reservation and reconcile the original run by its ID after exact Hatchet readback. The broker checks at most one due coding receipt every 30 seconds. It records a candidate from the original worker with the exact lane and approved base. A changed candidate or unavailable worker remains visible and leaves the repository reserved. A worker candidate is a patch ready for review. Exact-commit GitHub CI remains an independent verification gate. It cannot start a new coding job under the Nuc's grant. The host supervisor in `first-officer/standby_host.py` starts a child only with a broker lease, checks OpenClaw's loopback readiness, and renews only for a ready child. Startup grace is 20 seconds. A failed renewal parks the host and stops its child. A private watchdog pipe also closes if the supervisor crashes. The child guard then kills the gateway process group. A new launcher refuses an already occupied readiness port. The native gateway can use `standby_channel.py` as a loopback Telegram API with a locally generated placeholder token. The gateway's Telegram config uses that loopback `apiRoot`, `replyToMode: all`, and `FO_STANDBY_TELEGRAM=1` for the brokered typing and acknowledgment paths. The adapter forwards replies only for a pending original message. Use `standalone.py --standby-profile` with the usual pinned release paths to render the brokered service. The private profile contains `hostname`, `port`, `bot_id`, and absolute `cert`, `key`, and `ca` paths. Start from a token-free config template with one allowlisted Telegram owner. The launcher generates a temporary loopback config, starts its bridge before OpenClaw, and reports the broker epoch. The brokered service currently answers chat and status and declines interactive approvals and dispatch. Keep it in an isolated test service until interactive replies, proactive monitoring, and the integrated coding submit and completion path have passed their integration gates. In staged mode, the gateway reads broker reservations for `what needs me?`. The broker's submit API is covered by tests and the staged gateway still declines dispatch and interactive approvals. [OpenClaw health checks](https://docs.openclaw.ai/gateway/health) [OpenClaw Telegram reply behavior](https://docs.openclaw.ai/channels/telegram/messaging) [Python TLS documentation](https://docs.python.org/3/library/ssl.html#ssl.CERT_REQUIRED) [Telegram Bot API](https://core.telegram.org/bots/api#getupdates)

When neither gateway has polled the broker in the last two seconds, the broker can send typing and acknowledge a fresh, durably queued owner message. The fixed reply says the gateway will answer when ready. Both effects share the gateway’s receipts, so activation or an uncertain send cannot repeat them. The final answer still requires a ready, leased gateway.

Run `tests/first_officer/standby-joined.py` in a loopback-only Linux namespace with a supported First Officer bundle and the installed Node and OpenClaw executables. It runs both packaged launchers with separate certificates, uses the real 90-second deadline, and checks a queued command and stale-host rejection. This exercises the broker, native gateway and durable inbox. Physical Mac startup and real Telegram delivery require the BobTestesBot rehearsal before production. Verify a brief Nuc outage keeps the current epoch. Then test a Nuc outage exceeding 90 seconds, an isolated Nuc that can still reach Telegram, a returning Nuc with an old epoch, an unavailable witness, and a Mac reboot to the login window. Interrupt a reply and a queued job during transfer. Require one Telegram consumer, no duplicate job, and a correct Mac reply within the two-minute target without a local login, SSH password, or Bobserver. Record measured takeover and recovery times. Roll back by disabling promotion, parking the Mac and confirming its fence is closed before restoring the Nuc as the sole consumer. Automatic promotion remains pending.

### Start the broker at boot on an independent Linux host

The systemd template at `first-officer/systemd/openclaw-captain-broker@.service` starts the pinned broker at boot under the dedicated `openclaw-broker` system account. Confirm the host has systemd, Python 3, and local persistent storage before choosing it. The broker must be reachable from both Captain hosts while either one is offline. Keep the SQLite state under `/var/lib/openclaw-captain-broker` on the broker’s own disk.

Stage the approved release under `/opt/openclaw-fo/first-officer-<digest>`. Verify the complete release inventory before copying it to the host. Keep the release root-owned, immutable to the service account, and readable by that account. At every start the unit checks the manifest digest against the instance name and verifies the listed files. Use a root-owned `/etc/openclaw-captain-broker` directory accessible only to the administrator and service account. Its `broker.env` sets `BROKER_LISTEN` to the chosen private IPv4 interface. Install `ca.pem`, `broker.pem`, `broker.key`, `nuc.pem`, `mac.pem`, and the verified bot’s `telegram.json` there. The service account needs read access. The Telegram JSON must be owned by the service account and mode `0600`. Keep the broker private key readable only by that account. Open only the private broker port to the two Captain hosts.

Jeff activates the tested instance after confirming that the previous Telegram consumer is stopped. First use BobTestesBot and the rehearsed private credentials. From the reviewed checkout on the independent host, with `digest` set to the approved manifest SHA-256, run the following as the operator.

```bash
unit="openclaw-captain-broker@${digest}.service"
systemd-analyze verify first-officer/systemd/openclaw-captain-broker@.service
sudo install -m 0644 first-officer/systemd/openclaw-captain-broker@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "$unit"
sudo systemctl is-active "$unit"
```

The base unit starts lease and Telegram services. When the queue rehearsal passes on that host, a root-owned instance drop-in can replace `ExecStart` with the same command plus `--review-queue-config` or `--project-queue-config`, using private operator-approved files. Keep queue tokens in the private service environment.

To roll back, disable and stop that exact instance with `sudo systemctl disable --now "$unit"` and verify it is inactive before permitting a different consumer. Preserve the private authority and inbox state. If a prior reviewed instance can read that state, start it only after confirming the failing instance and both gateways are fenced. Reconcile unconfirmed deliveries before resuming.

For brokered coding, the operator’s `project-scope.json` must contain the exact `request`, `epoch`, an `expires` time within one hour, `worker` set to `local-qwen`, and `planSha256` for the private Nuc worker plan. The broker stores that hash on the repository reservation. A dedicated Nuc Hatchet worker can verify it via mutual TLS by setting `FO_PROJECT_BROKER_PROFILE` to its private, absolute broker profile path. Brokered mode requires a current Nuc lease and the original reservation and queue receipt. Existing reservations without a plan hash can still be inspected and reconciled. They cannot authorize another edit. Telegram admission for new brokered coding work still needs the live gate and GitHub evidence checks wired in.

### Standalone review queue

The standalone review uses the same Hatchet and Restate dispatcher from the [isolated queue pilot](QUEUE-PILOT.md). It requires a mode `0600` `queue.json` file beside the standalone authority. Use the queue policy format in [INSTALL.md](INSTALL.md#queue-a-review-lane) and set the Hatchet workflow to `fo-captain-review`, the Restate service to `CaptainReview`, and the handler to `verify`. Use a dedicated scope with a concurrency limit of one for the Restate review worker. Load the ingress credentials in the gateway's private service environment.

The packaged worker adapters are under `round6/runtime/segment1/queue-runtime/workers`. Point `FO_CAPTAIN_REVIEW_SCRIPT` at the absolute path of that release's `round6/runtime/segment1/review-worker.mjs`. Run the Hatchet adapter with `hatchet-sdk==2.0.0a6` in its own venv. Use `@restatedev/restate-sdk@1.15.0` beside a private copy of the Restate adapter and register its loopback endpoint with the local Restate node. The worker uses the local read-only GitHub CLI. Both adapters verify the exact queued head and return the lane, PR, head, and CI result. Neither uses a model. A queue acceptance receipt is shown while the worker runs. A missing or uncertain receipt stays visible and blocks a repeat dispatch or handoff until it is resolved.

The standalone registry probes the gate on its own host. Put a mode `0600` `gate.json` beside the authority with exactly `directory`, `lock` and `hold`. Use the absolute directory and two basenames from the installed local gate. The full gate must take an exclusive flock on that same existing file for the duration of the run. Keep the lock inode stable. The probe reads the hold marker and tests the lock without creating either file. The dispatcher holds a shared lock through submission and refuses a held or unverifiable gate before reserving a review. For local capacity, put a mode `0600` `capacity.json` beside the authority with `commands`, a nonempty list of exact executable basenames, and `limit`, the configured maximum number of tracked heavy processes. Use the actual names reported by the host's process table. The probe counts matching processes owned by the gateway account. The fleet reply labels that figure as tracked processes, and dispatch checks it again before submission. Other hosts' readings still come from fresh fleet snapshots.

For standalone `quota status`, put a mode `0600` `quota.json` beside the authority with a single `codex` field containing the absolute path to the local, signed-in Codex executable. This uses an on-demand quota query through the CLI, with no turn submission. The reply shows current local Codex windows and explicitly identifies provider windows that the local check cannot verify. The model-free CI review continues to use its reviewed queue policy for admission.

### Prepare the native service

For the Nuc's local quick-answer route, point the OpenClaw LiteLLM provider to its loopback proxy and list the approved alias in the provider model catalog. Give the conductor exactly one matching LiteLLM entry in allowedCompletionModels and include that entry in allowedModels. Use the proxy's private gateway lane key through the service environment. With only a local model, keep the fallback map empty. The Nuc rehearsal used one CPU model slot and two CPU threads.

Render the unit or plist into private scratch before approval. Use a private, host-specific OpenClaw config that points to the pinned candidate, an empty mode `0700` gateway state directory for a clean-host rehearsal, and a mode `0600` JSON object for the service environment. The environment file accepts the gateway LiteLLM key, the two queue lane tokens, and the private queue host list. The service definition contains only the file path. For the enabled Telegram profile, set the Captain plugin's `hooks.allowConversationAccess` to true and use the same single owner in `channels.telegram.allowFrom` and `channels.telegram.execApprovals.approvers`, with `dmPolicy: allowlist`. Set the queue policy's `token_env` names to `FO_HATCHET_LANE_TOKEN` and `FO_RESTATE_LANE_TOKEN`.

```bash
umask 077
python3 -B first-officer/standalone.py --node "$captain_node" \
  --release "$pinned_candidate" --python "$python_bin" --node-binary "$node_bin" \
  --openclaw "$openclaw_bin" --config "$private_gateway_config" \
  --state "$private_gateway_state" --environment "$private_gateway_environment" \
  > "$private_service_definition"
```

After the approved first-authority or import step, install the generated definition under the active host's user account. The Nuc uses its systemd user manager. The Mac Mini uses the GUI user's LaunchAgent. The Mac service can start when loaded. Keep both hosts assigned the same production bot account only under the guarded launcher. On transfer or rollback, stop the outgoing service before exporting its authority.

```bash
# Nuc first install and approved start.
install -d -m 0700 "$HOME/.config/systemd/user"
install -m 0600 "$private_service_definition" "$HOME/.config/systemd/user/openclaw-captain.service"
systemctl --user daemon-reload
systemctl --user start openclaw-captain.service

# Nuc stop before transfer.
systemctl --user stop openclaw-captain.service
```

```bash
# Mac Mini first install and approved bootstrap.
install -d -m 0700 "$HOME/Library/LaunchAgents"
install -m 0600 "$private_service_definition" "$HOME/Library/LaunchAgents/local.openclaw.captain.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.openclaw.captain.plist"

# Mac Mini unload before transfer.
launchctl bootout "gui/$(id -u)/local.openclaw.captain"
```

Standalone cutover gates still include the native Mac service rehearsal, binding the live gate, capacity and quota probes on each host, and the full Telegram checklist on each host. Verify both peer transfer and rollback through the generated services. Use the standalone checklist in [LIVE-TEST.md](LIVE-TEST.md) for both hosts. The Bobserver pilot and rollback below apply only to the legacy profile.

## Project briefing pilot

The standalone candidate can include up to two projects in **brief me** and **what needs me?**. A project profile is optional. Use an owner-only file named `projects.json` beside the standalone lease in the **isolated** authority directory for the pilot. Keep it out of source control. The gateway reads it on each request. The existing owner DM gate still applies.

```json
[
  {
    "repo": "rupret007/Cursor-OpenClaw-Integration",
    "issue": 40,
    "goal": "Make review delivery recoverable",
    "milestone": "Show unconfirmed delivery",
    "owner": "Jeff",
    "acceptance": "Captain surfaces the unresolved receipt",
    "scope": "read-only",
    "impact": 3,
    "urgency": 2,
    "risk": 0,
    "dependencies": []
  }
]
```

Set the profile mode to `0600` and its authority directory to `0700`. Dependencies are issue numbers in the same repo. Use values between 0 and 3 for impact, urgency, and risk. Higher impact takes precedence, followed by higher urgency and lower risk. Only rows with live evidence and actionable milestones compete for the next recommendation. A linked PR is optional. Set `pr` to its number and `checks` to the required GitHub Actions job names. Bob reads the PR head before and after reading checks and reports uncertainty when they differ.

The read-only profile can recommend work. It does not grant worker dispatch. The ledger stores the last 50 changed recommendations with the issue, action, observed commit when available, and lease epoch. Signed handoff carries the ledger. Each host loads its own operator-provided profile. To roll back the briefing on an approved test host, move its `projects.json` to a private backup on the same host. The next request uses the existing Captain briefing. Restoring the file restores the project view.

### Isolated coding admission

Use a private test authority and a dedicated test Hatchet workflow. The coordinator requires one `project-grant.json` in the authority directory. Its fields are `repository` in lowercase, `issue`, `head` as a full commit, `epoch`, `branch`, `worker` set to `local-qwen`, `planSha256` of the exact worker plan bytes, and `expires` as Unix time within the next hour. Keep grant and policy files at mode `0600`. A separately reviewed `project-queue.json` selects the dedicated `fo-captain-project` workflow.

The private `project-worker.json` contains `request` with the four identity fields, an approved local `checkout`, one tracked `file`, its exact `before` text, the operator-reviewed `prefix` and `suffix`, and the short model `prompt`. The worker quotes the model's replacement as one string literal inside that approved template. Keep worker plans and scratch worktrees private. Use the plan digest from `sha256sum project-worker.json` for the grant. The reviewed adapter uses `FO_PROJECT_PYTHON` for the pinned local-model environment and registers the coding workflow in the isolated Hatchet instance.

In the private owner conversation, use `captain this: advance owner/repo #40` for the exact approved issue. Operators can also pass the four identity fields as JSON to the state script's `submit-project` operation. The coordinator checks the live GitHub issue and branch head, local gate and capacity, current authority, and exact grant before saving the reservation or contacting Hatchet. One unresolved reservation blocks a second coding job for the repo and blocks standby transfer. The briefing can still recommend eligible work in another project.

After independently identifying an accepted Hatchet run, use `reconcile-project` with the original four identity fields and `run`. This checks the original queue receipt and restores the coordinator's running state without submitting again.

The project monitor inspects one eligible job at a time. The worker returns the lane, approved base, and candidate commit. Jeff reviews the isolated patch and decides whether to publish it. An operator who has an authorized PR can place a private `project-verification.json` beside the lease with the lowercase `repository`, `issue`, full candidate `commit`, `pr`, and `checks` of required GitHub Actions job names. Set mode `0600`. The verifier checks candidate ancestry, rechecks the open issue and base, and confirms the PR head both before and after reading exact-commit checks. Use `inspect-project` with the four identity fields to refresh the receipt on demand. The handoff labels a saved verdict as the last verification. The isolated worker creates its candidate commit in the lane's private worktree using Git plumbing. Review the committed file by comparing the candidate with the approved base. Leave the reservation in place while a queue outcome is unresolved. For rollback on a test host, remove the exact grant from the test authority after recovering active work, then stop the isolated coding worker. Keep the private receipt and candidate for review.

### Inspect a coding submission after interruption

The coding queue boundary uses a stable lane ID derived from the normalized repository, issue number, exact commit, and lease epoch. Only the dedicated Hatchet coding workflow can accept this payload. A refused coding submission needs an operator decision. A submission with unknown acceptance stays reserved while the original outcome is checked. Existing review lanes retain their Restate route.

Inspect a private test ledger without contacting the queue:

```bash
nice -n 10 python3 -B first-officer/lanes/queue.py "$test_policy" "$private_ledger" \
  --status --project-repository "$repository" --project-issue "$issue" \
  --head "$full_commit" --project-epoch "$epoch"
```

If the receipt shows `pending`, the status command establishes only what the local journal knows. Identify the original run in Hatchet and repeat the command with `--reconcile-project-run "$hatchet_run"` in place of `--status`. This uses the original private queue policy to read the run and checks its tenant, workflow, idempotency key, repository, issue, commit, and epoch before adopting the receipt. A mismatch leaves the run pending. The scoped coding worker and Telegram recovery rehearsal are separate acceptance gates.

### Rehearse an admitted command in isolation

The queue pilot can drive the packaged gateway through a private Telegram Bot API fixture. Set `FO_PROJECT_WIRE_RELEASE`, `FO_PROJECT_WIRE_NODE`, and `FO_PROJECT_WIRE_CLI` to the inventory-verified bundle, pinned Node executable, and installed OpenClaw entrypoint. Run `nice -n 10 bash tests/first_officer/queue-pilot/run.sh` from the Nuc in an isolated profile without a system GitHub CLI. The pilot supplies a private fixture adapter for GitHub and uses a loopback model stub.

The rehearsal waits for the native outbox to settle before a normal restart. When a send is interrupted and the remote receipt is unknown, treat delivery as unresolved and inspect the original outcome before repeating the request. Telegram final-text recovery can resend a reply after a lost acknowledgment. The scoped job reservation still fences worker execution. [OpenClaw Telegram recovery](https://docs.openclaw.ai/channels/telegram/troubleshooting)

### Verify an admitted reply on BobTestesBot

With an authorized test window, run `tests/first_officer/queue-pilot/test-bot-bridge.py` outside the fixture network namespace. Give it a Unix socket path inside a private scratch directory and the private BobTestesBot pairing file. Then set `FO_PROJECT_BOT_SOCKET` for the joined queue pilot. The bridge verifies the bot identity and accepts three fixed phases once each. Only the typing signal, acknowledgment, and synthetic job admission reach the paired chat. GitHub and model data stay inside the fixture. Record times from synthetic ingress. Stop the bridge after the run.

### Reconcile a Captain Telegram reply

Explicit project advances and handoffs reserve their incoming message identity in the private authority journal before work or acknowledgment. The final reply uses one direct Telegram send to the paired owner. An interrupted response leaves an unconfirmed receipt. A restart preserves the receipt and a repeated input reuses its reservation.

When **what needs me?** reports an unconfirmed delivery, inspect the private receipt times with `project-telegram-review` on the candidate state script. Open the original private conversation and determine whether the reply arrived. Use `resolve-project-telegram` with the receipt's full `key` and an observed `outcome` of `delivered` or `missing`. This records the observation without sending anything. For a fresh status request, use a new Telegram message. The journal stays with signed authority handoff. Ordinary Telegram replies still use the native recovery policy.

### Daily read-only project briefing

Enable the daily pilot on an isolated authority by placing `{"enabled":true}` in a private `project-daily.json` beside the lease and supplying its approved `projects.json`. The active Captain checks once per minute and generates the first eligible briefing at or after 07:00 Central Time. A restart after 07:00 can catch up. The saved day and observation time travel in the signed authority handoff.

The snapshot saves repository and issue identities, the recommendation, and the exact CI commit if available. No message is sent by the timer. Use **brief me** to refresh evidence on demand and **handoff** to see the date of the last saved briefing. To roll back the daily pilot, set the private profile's `enabled` value to `false`. Existing receipts stay available.

### Compare Captain performance

Use the same scoped task, initial GitHub state, and acceptance checks for each assistant. Use private fixtures for write and fault cases. Collect wall time from prompt receipt to the first correct actionable answer, the number of separate Jeff interventions, and the number of milestones with verified receipts. Record unrun cases as unverified.

| Case | What a passing outcome shows |
| --- | --- |
| Milestone triage | Chooses an actionable item from the live board and gives the reason |
| Two competing projects | Selects the higher-impact eligible action with both projects visible |
| Blocked dependency | Gives the current blocker and prioritizes remaining eligible work |
| Small implementation | Produces a reviewable change that satisfies independent checks |
| CI failure | Reports the failing check on the current commit |
| Stale PR head | Withholds the earlier verdict and rechecks the new head |
| Missing worker allowance | Stops before a model call and says what is missing |
| Restart during dispatch | Recovers the same run and reconciles its outcome |
| Duplicate delivery | Returns one stable outcome and produces one write |
| Approval required | Identifies the exact decision, scope, and revision Jeff must approve |

## Daily commands

| Send in the private owner chat | What OpenClaw does |
| --- | --- |
| `captain status` | Shows the shared lease holder, epoch, scope and pending decisions. |
| `brief me` | Shows the next decision or active review alongside current fleet, gate, GitHub and quota evidence. |
| `what needs me?` | Lists the scope decision, current blockers and any unconfirmed final delivery. |
| `handoff` | Records the current holder, work, evidence and next steps in the private captain state directory and replies in Telegram. |
| `captain this: verify CI for PR #33` | Identifies the exact Integration PR head and assigns one CI review when the prerequisites are verified. |

The current owner’s grant applies to one lease epoch. A changed epoch requires a new grant. A grant authorizes a local CI review and its owner-chat progress messages. GitHub writes, merges, deployment, gate requests, text sending and destructive work retain their existing approval requirements. Quota admission uses the exact runner’s account. An unavailable quota reading, a reached limit or available billing credits can block dispatch.

## Use Andrea in Bob the Bot

Send ordinary requests to Bob the Bot to use OpenClaw. Start a message with `@andrea` to address Andrea in the same private chat.

| Send | What Andrea does |
| --- | --- |
| `@andrea` | Shows the messaging command. |
| `@andrea summarize my texts` | Reads recent activity from approved chats and surfaces topics, questions and changes. |
| `@andrea what are my recent text messages?` | Runs the same live summary. |

Andrea’s access in this chat is read-only. It summarizes recent approved messages on the Mac and returns fixed labels and counts. `summarize my texts` also works as a shortcut.

## Diagnose Andrea before switching

From the reviewed PR checkout in WSL on the Nuc, use your existing Mac login. The diagnostic runs on the Mac and prints only bounded counts. It keeps the installed reader and gateway in service.

```bash
read -r -p 'Mac Mini SSH host: ' mac_host
read -r -p 'Mac Mini SSH user: ' mac_user
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --mac-host "$mac_host" --mac-user "$mac_user" --diagnose
```

Check `recentTextPreviews` against a real recent one-to-one thread privately. `freshChatMetadata` at zero with positive `recentTextPreviews` is evidence that the bounded preview fallback found activity missed by chat metadata. Zero accepted rows, a stale preview index or read errors require checking the Mac sync and eligibility counts before proceeding.

If the reader returns valid rows and zero recent previews, check the upstream channel and chat scope from the same shell. The command reports only fixed status labels and age categories.

```bash
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --mac-host "$mac_host" --mac-user "$mac_user" --diagnose-sync
```

Jeff can refresh the existing key’s forced command from this checkout. The reviewed reader uses fresh cached previews when available. When the cache is stale, it reads a bounded sample of exact threads on Andrea's confirmed allowlist through the configured BlueBubbles provider. It prefers eligible one-to-one threads and can use the approved groups when the allowlist supplies only groups and self chats. The Mac classifies each preview locally and sends counts and a fixed set of labels for topics, questions, requests and changes. The helper checks for recent preview evidence on the Mac before changing the key and checks the enriched summary from Bobserver afterward.

```bash
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --mac-host "$mac_host" --mac-user "$mac_user" --refresh-reader
```

After it reports `Andrea reader verified from bobserver.` and Jeff approves, activate the reviewed gateway release. Then send `@andrea summarize my texts` to **Bob the Bot**, the OpenClaw Telegram account. Use the [Telegram acceptance checks](LIVE-TEST.md) to compare its findings privately with a recent thread. Use the error line to diagnose a refused refresh. `no-authorized-chats` means the allowlist supplied no eligible non-self thread.

## Legacy Bobserver pilot

1. Review the PR and its current CI, then use the [single activation command](INSTALL.md#publish-and-activate-the-reviewed-candidate). Leave the lease with Grok Bot while running the [timed Telegram checks](LIVE-TEST.md). Privately compare the Andrea summary with the recent thread. The staged checklist, `python3 first-officer/pilot/rehearse.py checklist`, prints the live sequence and runs none of it.
2. In an SSH shell on Bobserver, verify the existing shared lease using the operator CLI.

   ```bash
   captain_cli="$HOME/work/lanes/openclaw-backup2-20261118-1340/conductor-captain"
   nice -n 10 "$captain_cli" status
   ```

3. Choose one open Integration PR with terminal CI. Authorize the scope and have Grok finish its current dispatch and record a fresh, genuine check-in through the normal process. From the reviewed checkout on Bobserver, use the epoch printed by `status` for the handoff. Takeover was off and the watchdog timer was inactive during diagnostics. The arming command checks the fresh check-in, explicit handoff, drained dispatches and exact epoch under the shared fence. Run one watchdog tick after it succeeds.

   ```bash
   read -r -p 'Current Grok-held epoch: ' grok_epoch
   nice -n 10 "$captain_cli" handoff "$grok_epoch" &&
     nice -n 10 python3 -I -B first-officer/captain-arm.py arm "$grok_epoch" &&
     systemctl --user start openclaw-conductor-watchdog.service
   nice -n 10 "$captain_cli" status
   ```

   Check the lease even if the watchdog service reports a failure. A Telegram delivery failure can occur after the lease changes.

4. Verify the CLI shows `first-officer` held and Telegram `captain status` shows OpenClaw. Send `captain grant integration-ci epoch N` with the **new** epoch, then submit the pilot PR. Send the same task twice. Check that the handoff and local runner receipts show one lane for that full head. Compare the final Telegram outcome with both required checks on GitHub. Check the lane receipt under `~/work/lanes/first-officer-codex/`.
5. Exercise the failure path using an approved open Integration PR with a known failed check or an operator-supervised failing worker. Keep the full tested head, the outcome and the elapsed time. Verify a reported failure remains in the handoff.
6. Send `captain pause epoch N`, try another in-scope task and verify that there is no new lane. Send `handoff` and verify it retains the work and next action. Send `captain handback epoch N`. Confirm the CLI shows Grok Bot again at a newer epoch and a command using the old epoch is rejected.
7. After the pilot and live checks pass, Jeff can explicitly approve OpenClaw as primary for this scope. Repeat the fenced handoff and grant at the new epoch. Capture the live `captain status` and record the accepted scope and verified receipt in `STATUS.md`.

## Legacy Bobserver handback and rollback

Send `captain pause epoch N` to stop new captain-lane submissions. A review already started can finish and remains visible in the handoff. Send `captain handback epoch N` to transfer the shared lease after the fence drains.

If Telegram is unavailable, request handback on Bobserver and run one watchdog tick. Keep the service and its current approval policy in place until the CLI confirms the new holder.

```bash
captain_cli="$HOME/work/lanes/openclaw-backup2-20261118-1340/conductor-captain"
nice -n 10 "$captain_cli" status
nice -n 10 "$captain_cli" request-handback &&
  systemctl --user start openclaw-conductor-watchdog.service &&
  nice -n 10 "$captain_cli" status
```

To restore the original disarmed state after handback, use the **new Grok-held epoch** from the operator CLI in the reviewed Bobserver checkout.

```bash
read -r -p 'Current Grok-held epoch: ' grok_epoch
nice -n 10 python3 -I -B first-officer/captain-arm.py disarm "$grok_epoch"
```

After handback, use the private installation backup with the [gateway rollback commands](INSTALL.md#roll-back-to-the-exact-prior-config-and-pin). If the Mac reader was refreshed, restore its previous restricted command from the Nuc.

```bash
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --mac-host "$mac_host" --mac-user "$mac_user" --restore-reader
```
