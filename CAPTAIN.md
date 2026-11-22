# OpenClaw captain on Telegram

OpenClaw can captain exact-head CI reviews for open PRs in Cursor-OpenClaw-Integration. The pilot scope is `integration-ci`. Each review uses one isolated lane on the existing subscription runner. OpenClaw checks the live fleet, gate, runner quota and required GitHub checks before dispatch. It follows a pending CI head for up to 20 minutes and verifies the current head again after the worker exits.

The staged pilot and the staged pin restore are proven on a private copy. They do not read the live lease and they do not stop the gateway. Production authority stays with the current holder until the live checklist on this page is run. Gateway activation is one command, `python3 -B first-officer/release.py`, with one rollback. `python3 first-officer/archive.py pack` writes a checksummed copy of this lane for offline testing. That copy is not the gateway release.

## Independent Mac Mini and Nuc profiles

The Mac Mini and Nuc are the standalone Captain targets. Each needs its own gateway, Captain state, queue and LiteLLM route. Andrea runs on the Mac Mini. Bobserver is an optional fleet member and an opt-in compute worker. Run the full acceptance suite on each host with Bobserver unreachable. Use a separate test Telegram account for the rehearsal and a no-cost model route or loopback provider fixtures.

Set `FO_LOCAL_NODE=mac-mini` for the Mac gateway or `FO_LOCAL_NODE=nucbox` for the Nuc gateway. Local load uses that host's label. Local job counts and Captain admission require a fresh snapshot whose `host.node` matches that setting. The status publisher accepts `local_node` in its input. Remote fleet samples still need fresh timestamps. On the Mac, the gateway reads Andrea locally under the Andrea LaunchAgent account. On the Nuc, it connects to the same read-only Mac reader over a dedicated SSH route.

The independent reader route uses a dedicated Nuc key and a pinned Mac host key. After approval to add that key to the Mac, run from the Nuc checkout under the same Linux account that will run the gateway:

```bash
read -r -p 'Mac Mini SSH host: ' mac_host
read -r -p 'Mac Mini username: ' mac_user
nice -n 10 python3 -B first-officer/provision-andrea.py \\
  --local-client --mac-host "$mac_host" --mac-user "$mac_user"
```

The local provisioning path connects only to the Mac. The Mac has separate restricted entries for the Nuc and the incumbent reader. The Nuc key can run one read-only summary command. Refreshes and restores on the Nuc use the same `--local-client` flag.

### Standalone authority rehearsal

The supported standalone launcher checks the local lease and holds an exclusive lock for the lifetime of the gateway process. A transfer requires the releasing gateway to exit and its reviews to settle. The source parks before issuing a signed handoff. The recipient saves the existing work and replay history before accepting a newer epoch. Jeff grants the review scope again at that epoch. The same export can be recovered after an interrupted copy. A parked host refuses gateway startup. Rollback transfers authority in the reverse direction at a newer epoch.

Rehearse with the [authority tests](tests/first_officer/test_captain.py). They use separate scratch directories and real OpenSSH Ed25519 signatures. The production launcher and operator commands below use the installed candidate, and require the approved two-host setup. Keep both host keys private and exchange only their verified public keys, saved as `peer.pub` with mode `0600`. Place each host's own key at `~/.local/state/openclaw-captain/handoff-key` inside a mode `0700` directory. After confirming the old production Telegram consumer has stopped and Jeff approves the transfer, initialize only the Mac Mini.

```bash
# Run from the Mac Mini under the intended gateway account.
export FO_LOCAL_NODE=mac-mini
printf '{}\\n' | python3 -I -B "$captain_state" local-initialize

# The launchd or systemd service runs this command under its local account.
python3 -I -B "$captain_state" local-run "$openclaw_bin"
```

Here `captain_state` is the candidate's `round6/runtime/segment1/captain-state.py` and `openclaw_bin` is the absolute path of the installed OpenClaw executable named `openclaw`. The service environment selects its own `FO_LOCAL_NODE`. Never launch the same bot account outside the guarded service. A lost or restored authority directory needs operator reconciliation with both hosts, because an offline peer cannot detect that the other host restored old state.

For a planned transfer, pause and drain the review scope, stop the current gateway service, and obtain its exact epoch from the local authority view. Save the handoff as a private file and copy it to the paired host over a trusted connection. Keep it out of logs and source control.

```bash
umask 077
printf '{}\\n' | python3 -I -B "$captain_state" view
printf '{"epoch":%s}\\n' "$captain_epoch" |
  python3 -I -B "$captain_state" local-release > "$private_handoff"

# Run under the destination account with its own FO_LOCAL_NODE.
python3 -I -B "$captain_state" local-import < "$private_handoff"
```

Standalone cutover gates are still open. Both hosts need a queue-backed review runner, verified local gate and quota sources, and a clean-host installer. The Mac needs a launchd rehearsal and the Nuc needs a systemd user-service rehearsal. Verify the guarded launcher and full Telegram suite on each host, including a peer transfer and rollback. The existing Bobserver install procedure serves the legacy profile.

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

## Activate and run the supervised pilot

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

## Take back control and roll back

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
