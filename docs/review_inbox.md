# Review inbox writer note

The web host reads `reviewInbox.dir` from its private configuration. It publishes only `reviewInbox: { enabled: true }` to the renderer. The directory contains `items.json` (CLI writer), `acks.json` (web writer), and `.txn.lock` (shared transaction lock). Keep this directory outside every checkout and deploy directory. The TV feed `action-items.json` is unrelated.

The rail is present on the ordinary desktop and phone web views. Chat popouts hide the rail, its scrim, and its toggles without reserving layout space. A transaction notice appears above any stale or capacity status so save failures and uncertain writes remain visible with the feed warning.

The browser gives each acknowledgement request a 15-second deadline and retries a possibly accepted operation with the same operation ID only while its first send is less than 23 hours old. After that, it refreshes and shows an uncertain-write notice. A new click creates a new operation ID. An unresponsive request cannot hold the control busy indefinitely.

At release, install `scripts/review-item.js` and `main/review_store.js` as a read-only, versioned copy under `lews/bin/review-item-<candidate-sha>/`, with a stable `lews/bin/review-item` shim pinned to that copy and to the private inbox directory. The candidate includes `scripts/install-review-item.js` for isolated rehearsal. A web host rollback leaves the shim and v1 item/v2 ack files in place. Changing the shim or CLI version requires a separate exact-scope release step.

Lews should place this process note in his own `pentacle-review-inbox` runbook or memory entry after release:

> Add an item with every Pentacle answer to a Sai question, every artifact or link sent for review, and every question needing Sai's input. Never add status updates, progress notes, FYIs or acknowledgements. Use the actual structured `--reply-to-message-id` when known; free-text origin belongs in `--origin-note`. Acknowledging with “Got it” means received or reviewed only. It never approves a decision, completes an external task, or grants permission to execute one. `resolve` is Lews's item closure and does not edit Sai's acknowledgement history.
>
> `review-item add --kind review --title 'Review the draft' --link https://example.org/draft`
>
> Use `--project Agenteach|DUMBO|Pentacle|Personal|Other` when known. Omit it for Other. The inbox groups pending items by project; Done and Resolved remain global. A wrong project is corrected by resolving the item and adding a fresh one.
>
> `review-item add --kind answer --title 'Answer about the draft' --reply-to-message-id optimistic_local_example`
>
> `review-item add --kind input --title 'Choose the review window'`
>
> `review-item list --state pending`, `review-item list --state all --json`, `review-item resolve ri_<id> --reason 'Answered in chat'`

Seed only once under Lews's exact GO. The supplied 14-row seed is a test fixture in the candidate, not the live seed. Review and approve the final seed file and its hash before the live transaction; drop resolved rows and add known message IDs beforehand. A seed rerun with identical bytes is a no-op. A changed file or extra item is a conflict.

If a store is invalid, the UI shows an error and disables writes. Nonregular `items.json` or `acks.json` files, including named pipes, are refused promptly. An item file whose `updated_at` is more than five minutes in the future or more than five minutes newer than its mtime is also refused by both reads and mutations; a stale but otherwise valid file remains writable. Refusal leaves the target and backup files unchanged. Do not substitute `.prev` automatically. The lock times out after five seconds and is never stolen automatically. Recovery requires confirming the lock owner's death and inspecting both files and their backups. The 80% warning is `Inbox nearing capacity`; old receipts can be evicted only after 24 hours, while history is retained. Archive through a separately designed procedure before the hard cap.
