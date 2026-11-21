# First Officer Segment 1 Telegram acceptance

Jeff runs these in the private owner chat after activation. Use a stopwatch from Send to the visible reply. Look for Telegram typing and the single `On it.` line within **2 seconds** of each message. Record only the case, elapsed times and pass or fail. The memory case has two messages and one confirmation tap.

| Case | Telegram message | Expected live result | Final target |
| --- | --- | --- | --- |
| 1. Fleet | `what's running / load on each machine` | Bobserver, Mac Mini, NucBox and MacBook Pro each show measured 1/5/15-minute load, a Central Time observation and available tracked job counts. Cross-check with the current fleet collector. | <30 s |
| 2. Gate | `gate status` | The current gate state with an observation time. If a full gate is live, compare the head with the gate receipt. Confirm that a known active hold is reported. | <30 s |
| 3. GitHub | `my open PRs and CI` | The current open PR count for the owner, with recent PR identifiers, heads and CI state bound to those heads. Compare with GitHub. An empty list passes only when GitHub reports zero open PRs. | <30 s |
| 4. Memory | `remember that my segment marker is orange triangle` | A Remember card for the synthetic test fact. Tap **Remember** and verify the card changes to a saved confirmation. | <15 s to card and <15 s from tap to confirmation |
| 4. Recall | `what did I tell you about segment marker?` | The private saved fact is retrieved from the store and matches the synthetic marker. | <15 s |
| 5. Andrea | `@andrea summarize my texts` | Use a recent approved thread with a known question, request, or plan change. Compare the reply privately with that thread. Check that it identifies the scope, surfaces an accurate discussion or follow-up signal, and gives the correct posting order if shown. Require a qualitative finding as well as correct counts. Check for names, contact details, and raw excerpts. Their presence fails the case. | <30 s |

Also send `define a branch` to check a simple model answer in **<15 seconds**, and `quota status` to check current quota receipts in **<30 seconds**. The same acknowledgment target applies.

Send `@andrea` on its own to check the add-on help, then send `gate status` to confirm OpenClaw still handles an ordinary request in the same chat.

A timeout, unavailable source, stale sample or missing reply is a failed live case even when the bot reports it correctly. For a past-due check, record whether typing appeared, whether the acknowledgment arrived, whether a final reply arrived and which source was unavailable. Avoid copying replies, contacts or activity details into the report.
