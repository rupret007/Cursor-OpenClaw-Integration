# What needs Jeff

[TEST-NOW.md](TEST-NOW.md) covers the offline checks. [PR #33](https://github.com/rupret007/Cursor-OpenClaw-Integration/pull/33) carries the original Segment 1 work. [PR #36](https://github.com/rupret007/Cursor-OpenClaw-Integration/pull/36) carries the combined release. [PR #37](https://github.com/rupret007/Cursor-OpenClaw-Integration/pull/37) follows #36 with the Cursor plan checks. The planned merge order is #33 → #36 → #37, with Jeff's approval before each merge to main. Use the full approved commit for both checkouts.

## 1. Approve and activate the combined release

Build on the Nuc from the retained `first-officer-61ff4565dd5638a886effefbd720c92a17b7388acbc6eeaffb7779f78d3f1b36.tar` in the private `OpenClawFirstOfficer/segment1` directory under Windows LocalAppData. From WSL, extract it into a private directory and set `PRIOR_BUNDLE` to the extracted directory containing `SHA256SUMS`. In the clean reviewed checkout, set `REVIEWED_HEAD` to the approved full Git commit, create a private staging directory with `staging="$(mktemp -d)"`, and run `nice -n 10 python3 -B first-officer/release.py build "$PRIOR_BUNDLE" "$staging" --reviewed-head "$REVIEWED_HEAD"`. Keep the digest and private tar printed by the build. Transfer that tar to `~/.openclaw/segment1-staged/` on bobserver. The offline review archive is a separate artifact.

On bobserver, use the gateway owner's terminal in the same clean reviewed checkout. Load `OPENCLAW_LITELLM_API_KEY` through the approved private environment in the operator shell and the gateway service. Use the existing acceptance directory with a verified, nonempty audit chain. The installed verifier at `/opt/openclaw-fo/verify-install.py` must be a readable `root:root` regular file with group and other write access disabled. The activation command checks that before it touches the gateway.

Paste this block. It prints the activation and conditional rollback plans before asking you to type `ACTIVATE`. You can preview it in the local reviewed checkout by entering a dummy 64-character lowercase hex release digest and dummy absolute acceptance path, then pressing Enter at the confirmation. That runs only the dry-run command and needs no tar, key, Bobserver connection or sudo.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  read -r -p 'Approved full Git commit: ' reviewed_head
  [[ "$reviewed_head" =~ ^[0-9a-f]{40}$ ]]
  [[ "$(git rev-parse HEAD)" = "$reviewed_head" ]]

  read -r -p 'Approved release digest: ' release
  [[ "$release" =~ ^[0-9a-f]{64}$ ]]
  read -r -p 'Existing acceptance directory on bobserver: ' root
  export FO_ACCEPTANCE_ROOT="$root"
  baseline=61ff4565dd5638a886effefbd720c92a17b7388acbc6eeaffb7779f78d3f1b36
  archive="$HOME/.openclaw/segment1-staged/first-officer-$release.tar"
  backup_parent="$HOME/.openclaw/backups/fo-combined-$release-$(date +%s)-$$"

  nice -n 10 python3 -B first-officer/release.py --dry-run activate \
    "$release" "$archive" "$backup_parent" --from-release "$baseline"
  read -r -p 'Type ACTIVATE on bobserver to switch this release: ' confirm
  [[ "$confirm" = ACTIVATE ]] || exit 0

  [[ -z "$(git status --porcelain --untracked-files=all)" ]]

  : "${OPENCLAW_LITELLM_API_KEY:?Load the approved gateway lane key into this shell}"
  export OPENCLAW_LITELLM_API_KEY
  test -r "$archive"
  test -d "$FO_ACCEPTANCE_ROOT"
  mkdir -p -- "$HOME/.openclaw/backups"
  mkdir -m 0700 -- "$backup_parent"
  nice -n 10 python3 -B first-officer/release.py activate \
    "$release" "$archive" "$backup_parent" --from-release "$baseline"
)
```

Activation checks the running gateway, GitHub owner login, the restricted Andrea reader and the candidate inventory. It snapshots the exact prior config and pin, installs and verifies the root-owned audit seal, then publishes the candidate read-only. With the store copy in place, the installed verifier receives the saved candidate config, approved digest, and pin's rollback slot. A verifier failure stops the release before the gateway switches. The installer waits for `/readyz` and attempts to restore the prior pair if readiness fails. If the switch succeeds and its final service status check fails, the wrapper attempts one rollback and returns failure even after recovery.

The exact installed verifier invocation before the switch is `python3 -I -B /opt/openclaw-fo/verify-install.py "$backup/candidate-openclaw.json" "$release" 4759047e8a16d1691499d55c4b6656882e67bc2275a3297d2283aa9b01538753`.

Keep the printed private rollback directory. A manual rollback uses the saved config and pin and needs no acceptance root. The seal and published bundle stay on disk.

```bash
read -r -p 'Private rollback directory printed by activation: ' backup
nice -n 10 python3 -B first-officer/release.py rollback "$backup"
```

## 2. Run the timed Telegram acceptance checks

After a successful activation, run [LIVE-TEST.md](LIVE-TEST.md) in Bob the Bot's private owner chat. Check fleet, gate, PRs and CI, memory save and recall, quota, a simple model answer and Andrea. Send `@andrea summarize my texts`, then test `summarize my texts` and `summarise my texts` without the mention. Check for useful, accurate findings from a recent approved thread. Record only each case, typing and acknowledgment within two seconds, elapsed time and pass or fail.

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

The staged drill restored a file on disk. A live rollback stops and starts `openclaw-gateway.service`. For that check, arrange a separate window and use the private backup and [rollback command in section 1](#1-approve-and-activate-the-combined-release).

## 5. Install the root-owned audit anchor during activation

You run this on bobserver while activating. It is the sudo step that creates or updates the audit anchor. The file is `/var/lib/openclaw-fo/audit-anchor`. The directory is `/var/lib/openclaw-fo/`. Both are `root:root`. The directory is mode `0755`. The file is mode `0444`, so the OpenClaw service user can read it and cannot write or replace it. Mode `0600` is not used, because the service must be able to read the file. Only this root step creates or updates the file. Do not point `FO_AUDIT_ANCHOR` at it. That variable is still only an owner-writable copy, and a user-writable copy does not bless a new chain while this file is required.

Before publishing, activation runs `export-anchor` to create the private seal payload in the rollback directory. The audit key stays in the acceptance directory. The sudo steps install the directory and seal with the ownership and modes above. Activation then verifies the sealed chain. The activation preview lists each command.

For inspection, the dry run shows these exact seal commands after it exports the payload. They are part of activation.

```text
sudo install -d -o root -g root -m 0755 /var/lib/openclaw-fo
sudo install -o root -g root -m 0444 "$anchor_payload" /var/lib/openclaw-fo/audit-anchor
sudo chown root:root /var/lib/openclaw-fo /var/lib/openclaw-fo/audit-anchor
sudo chmod 0755 /var/lib/openclaw-fo
sudo chmod 0444 /var/lib/openclaw-fo/audit-anchor
```

Success from `export-anchor` is `Audit anchor payload written.` The sudo commands print nothing and exit 0. Success from `verify` is `Audit chain verified: <n> lines.`

`export-anchor` runs `verify` before it writes a payload. It fails closed, exits 1, and writes nothing when `verify` would reject the chain, including a forged `memory.json`, `pending.json`, or `receipt-lane-review.json`. It also fails closed when a required seal is missing, replaced, not owned by root, or not mode-restricted, including mode `0666`. The first installation can export only when `/var/lib/openclaw-fo` does not exist yet, `FO_PRODUCTION_AUDIT_ANCHOR` is unset, and `verify` accepts the chain. After the seal exists, export succeeds only for that chain or a later extension `verify` accepts, so these sudo commands can update the seal. An empty chain, sequence `-1`, is not sealed. The seal anchors history only through the last sudo install. Events appended after that install can be removed until the next one. Events at or before a sealed sequence of zero or higher cannot.

If export, verify, or the installed verifier fails closed, stop. Do not publish the candidate and do not switch the gateway. The command exits 1. The message says the anchor is missing, was replaced, is not owned by root, or is not mode-restricted. Leave `/var/lib/openclaw-fo/audit-anchor` in place. Deleting the acceptance directory, the trust directory, the witness directory, `root.id`, `anchor.json`, or replacing a user-writable `FO_AUDIT_ANCHOR` file does not bless a new empty chain while this root-owned file remains. Do not delete that file to start a new chain. Initialize a new acceptance directory only before the file exists. After it exists, run these sudo commands again only when `verify` would accept the current chain and that chain is the history you intend to keep.

## 6. Verify live Cursor allowance

On the Nuc, check the Cursor dashboard in the existing signed-in profile that the Cursor CLI will use. Confirm that its current included-plan period and all billing switches match the receipt. Keep the verified account binding and the private mode-0600 receipt outside the repo, and use the matching binding when running the tracker and fallback. Leave the reading unread if the dashboard requires a new token. See the [fixture format and expected tracker result](TEST-NOW.md#4-step-2-codex-claude-and-cursor-usage-tracking) and [operator setup](INSTALL.md#track-codex-claude-and-cursor-usage-from-a-lane).
