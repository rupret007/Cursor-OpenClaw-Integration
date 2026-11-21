# What needs Jeff

Everything else on this branch is already proven offline and in CI. The items below are the only ones that touch the live gateway, Telegram or production authority, so each one waits for you. Nothing on this list has been started.

Before any of it, you can verify the finished work yourself with [TEST-NOW.md](TEST-NOW.md). None of those checks touch anything live.

## 1. Approve and activate the combined release

One digest, one command, one rollback. Build the candidate on the Nuc, read the digest, then activate on bobserver.

```bash
python3 -B first-officer/release.py build "$PRIOR_BUNDLE" "$staging"
python3 -B first-officer/release.py activate "$release" "$archive" "$backup_parent"
```

Preview either one with `--dry-run` first; it prints the exact ordered commands and runs none of them. Activation refuses to start unless the gateway is already running, the GitHub CLI is authenticated as the owner account and the existing Andrea read-only bridge answers. It publishes the candidate read-only under its digest, records a private rollback directory, switches, and waits for readiness. It stops at the first failure.

The rollback is the printed private backup directory.

```bash
python3 -B first-officer/release.py rollback "$backup"
```

What changes when this is active: the `@andrea` prefix in your private owner chat routes to the existing restricted Mac reader, and the gateway's routed model traffic uses its own budgeted LiteLLM lane key instead of the shared credential.

## 2. Run the timed Telegram acceptance checks

After the release is active, work through [LIVE-TEST.md](LIVE-TEST.md) in the private owner chat and report which checks pass. These are the first messages that reach the live bot, so they need you at the keyboard. The check that previously failed was the Andrea text summary; the current release answers the untagged command `summarize my texts` and its common spelling variants through the read-only reader.

## 3. Approve the supervised authority pilot

The lease, watchdog, epoch handoff, pause, handback, stale-grant rejection and disarm all passed a private rehearsal on the Nuc against the installed fence and watchdog code. Production authority is still held by Grok Bot and nothing has moved.

The pilot needs three things from you: a fresh Grok Bot check-in, an explicit handoff at the expected epoch, and the named Integration CI scope granted at that epoch. [CAPTAIN.md](CAPTAIN.md) has the operator commands. Keep the first pilot to one review lane on one pull request head, and use the documented pause and handback to end it.

## 4. Confirm the rollback drill window

Before the pilot, the prior release and pin should be restored once on a staged copy and timed, so the real rollback has a measured duration rather than an estimate. This needs a staged copy of the current bundle and a few minutes when nobody is relying on the gateway. Tell us when that window is and we will script it to run unattended against the copy only.
