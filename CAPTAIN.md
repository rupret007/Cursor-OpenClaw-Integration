# OpenClaw captain on Telegram

OpenClaw can captain exact-head CI reviews for open PRs in Cursor-OpenClaw-Integration. The pilot scope is `integration-ci`. Each review uses one isolated lane on the existing subscription runner. OpenClaw checks the live fleet, gate, runner quota and required GitHub checks before dispatch. It follows a pending CI head for up to 20 minutes and verifies the current head again after the worker exits.

## Daily commands

| Send in the private owner chat | What OpenClaw does |
| --- | --- |
| `captain status` | Shows the shared lease holder, epoch, scope and pending decisions. |
| `brief me` | Shows the next decision or active review alongside current fleet, gate, GitHub and quota evidence. |
| `what needs me?` | Lists the scope decision, current blockers and any unconfirmed final delivery. |
| `handoff` | Records the current holder, work, evidence and next steps in the private captain state directory and replies in Telegram. |
| `captain this: verify CI for PR #33` | Identifies the exact Integration PR head and assigns one CI review when the prerequisites are verified. |

The current owner’s grant applies to one lease epoch. A changed epoch requires a new grant. A grant authorizes a local CI review and its owner-chat progress messages. GitHub writes, merges, deployment, gate requests, text sending and destructive work retain their existing approval requirements. Quota admission uses the exact runner’s account. An unavailable quota reading, a reached limit or available billing credits can block dispatch.

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

Jeff can refresh the existing key’s forced command from this checkout. The reviewed reader uses fresh cached previews when available. When the cache is stale, it reads a bounded sample of exact threads on Andrea's confirmed allowlist through the configured BlueBubbles provider. It prefers eligible one-to-one threads and can use the approved groups when the allowlist supplies only groups and self chats. The helper checks for recent preview evidence on the Mac before changing the key and checks for it again from Bobserver afterward.

```bash
nice -n 10 python3 -B first-officer/provision-andrea.py \
  --mac-host "$mac_host" --mac-user "$mac_user" --refresh-reader
```

After it reports `Andrea reader verified from bobserver.`, activate the reviewed gateway release. Then send `summarize my texts` to **Bob the Bot**, the OpenClaw Telegram account. The current gateway release recognizes the approved-group summary. Compare the result privately with a recent thread. Use the error line to diagnose a refused refresh. `no-authorized-chats` means the allowlist supplied no eligible non-self thread.

## Activate and run the supervised pilot

1. Review the PR and its current CI, then use the [gateway activation commands](INSTALL.md). Leave the lease with Grok Bot while running the [timed Telegram checks](LIVE-TEST.md). Privately compare the Andrea summary with the recent thread.
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
