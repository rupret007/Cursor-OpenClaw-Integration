# What needs Jeff

The offline work recorded in [STATUS.md](STATUS.md) can be checked with [TEST-NOW.md](TEST-NOW.md). The items below are the ones that touch the live gateway, Telegram or production authority, so each one waits for you. None of these live items has been started.

Before any of it, you can verify the finished work yourself with [TEST-NOW.md](TEST-NOW.md). None of those checks touch anything live.

## 1. Approve and activate the combined release

One digest and one activation command. The rollback restores the prior config, pin, and service. It does not delete the published candidate or undo a separately provisioned reader or proxy. Build the candidate on the Nuc from the private prior bundle, read the digest, then activate on bobserver. The review archive from `first-officer/archive.py` is the offline copy of this lane. It is not this activation.

```bash
python3 -B first-officer/release.py build "$PRIOR_BUNDLE" "$staging" --reviewed-head "$REVIEWED_HEAD"
python3 -B first-officer/release.py activate "$release" "$archive" "$backup_parent"
```

Preview either one with `--dry-run` first; it prints the exact ordered commands and runs none of them. Activation refuses to start unless the gateway is already running, the GitHub CLI is authenticated as the owner account and the existing Andrea read-only bridge answers. It compares the manifest digest with the approved release and records the private rollback before it publishes the candidate. It then publishes the candidate read-only under its digest, switches, and waits for readiness. It stops at the first failure.

The private rollback directory is printed only after that backup exists. A failure before then does not name a missing path.

```bash
python3 -B first-officer/release.py rollback "$backup"
```

What changes when this is active: the `@andrea` prefix in your private owner chat routes to the existing restricted Mac reader, and the gateway's routed model traffic uses its own budgeted LiteLLM lane key instead of the shared credential.

The checkout does not contain the installed verifier `/opt/openclaw-fo/verify-install.py`. Activation calls it with the same three arguments as the service pin: the config path, the release digest, and the rollback slot. It does not pass the unpacked directory. `verify-candidate` runs that command after the candidate files are in place with their installed read-only permissions and before the gateway is switched. A non-zero exit leaves the gateway running and removes the rejected directory only if the complete prior config and pin are still in place and there is no switch journal. You can run the same read-only check yourself before the switch. It must exit 0:

```bash
python3 -I -B /opt/openclaw-fo/verify-install.py "$backup/candidate-openclaw.json" "$release" 4759047e8a16d1691499d55c4b6656882e67bc2275a3297d2283aa9b01538753
```

A non-zero exit means that installed program rejected the candidate. Leave the gateway on the current release and do not run the switch. If the candidate might already be in use, `verify-candidate` preserves its store directory.

## 2. Run the timed Telegram acceptance checks

After the release is active, work through [LIVE-TEST.md](LIVE-TEST.md) in the private owner chat and report which checks pass. These are the first messages that reach the live bot, so they need you at the keyboard. The check that previously failed was the Andrea text summary; the current release answers the untagged command `summarize my texts` and its common spelling variants through the read-only reader.

## 3. Approve the supervised authority pilot

The staged failover passed on November 20, 2026. A missed 300 second check-in returned a private lease to captain at epoch 14 and rejected the old epoch. That rehearsal does not read the live fence. Production authority is still held by captain, and the live steps below have not been started.

Run the staged command in [TEST-NOW.md](TEST-NOW.md), then the live checklist. Stop at the first line that does not match.

```
1. Leave the live lease with captain until this staged rehearsal has passed.
2. Run python3 first-officer/pilot/rehearse.py rehearse --root /tmp/fo-pilot --epoch 12 --now 1763700000.
3. On bobserver, read captain status. Stop if the holder is not captain.
4. Require a fresh captain check-in and an explicit handoff at that epoch.
5. Arm that epoch and run one watchdog tick. Stop unless first-officer holds the next epoch.
6. Grant integration-ci at the new epoch, submit one pull request, and confirm a repeat is one lane.
7. Pause that epoch and confirm a new task creates no lane.
8. A missed 300 second check-in must return the lease to captain at a newer epoch and reject the old epoch.
9. Hand back at the current epoch, confirm captain holds a newer epoch, and disarm there.
10. Send no message from this checklist. Telegram checks stay on the live-test list.
```

The operator commands for steps 3 through 9 are in [CAPTAIN.md](CAPTAIN.md). Keep the first live pilot to one review lane on one pull request head.

## 4. Confirm a live rollback window

The staged file restore already ran. On November 20, 2026 it printed `Restored the prior pin in 114 us. The gateway was not touched.` That number is the copy on disk, not a gateway restart. A live rollback still stops and starts `openclaw-gateway.service`, and that has not been run. Use the private backup from an activation when you want that window.

```bash
python3 -B first-officer/release.py rollback "$backup"
```

## 5. Decide where the audit anchor lives

The acceptance audit refuses a new empty chain after the acceptance root, the trust directory, the witness directory, and `root.id` are deleted, as long as its anchor file is still there. That file is mode 0600 and this user can delete it. It is not a trust anchor. Offline checks do not put it on another machine or under another user.

With `FO_AUDIT_ANCHOR` unset, the file is `anchor.json` beside the trust base. Deleting that file as well lets `init` create a new empty chain that `verify` accepts. Set `FO_AUDIT_ANCHOR` to a file outside the acceptance root, its parent, and those three directories if a missing file should fail closed. Create the empty file, mode 0600, in a mode 0700 directory you own, before the first `init`. A missing or mismatched file does not verify as an empty continuation. Replacing it with a new empty file still lets `init` start a new chain. The path only preserves history if this user cannot create or delete it. That placement is your decision.
