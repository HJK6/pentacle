# Capability dashboard boundary

The desktop may retain a generic capability dashboard, but a public build must show only data supplied by a reviewed adapter. If the adapter is absent, the dashboard reports `unavailable` instead of reconstructing private project state.

## Public row contract

Rows may contain a stable id, repository label, topic, status, owner-neutral display label, progress summary, and next action. Synthetic rows with unresolved metadata belong in an `Unresolved` section. Unknown statuses belong in an `Other` column so a typo is visible.

## Renderer behavior

Client-side filters may reapply repository, host label, status, and search text to an already-fetched batch. All text is escaped. A primary action is one of `unresolved`, `drive`, or `spawn_additional`; the renderer does not infer authority from a row.

## Capability states

The adapter reports `healthy`, `degraded`, or `disabled` with a bounded explanatory message. A disabled or degraded subsystem does not expose a private watcher, shared-memory path, remote service, or deployment command.

Use synthetic rows and `coordinator` through `satellite` labels in tests. The exact data source and request names belong to the public daemon contract when that adapter is implemented.

## Request identity and commit freshness

Spawn, spec attach/detach, QA and report attestation use request-local identity
views. Every new capture enumerates permitted work folders and checks file path,
device, inode, size, mtime and ctime. Unchanged documents reuse parsed identity
inputs; snapshots never reread live identity documents. Polling works without a
watcher. Metadata work remains proportional to the folder count.

Overlapping snapshot captures share one rebuild. Async snapshot admission is
bounded to 16 callers; Store single/batch compatibility resolution has at most
eight admitted workers and no additional waiting queue. Overload or incomplete
batch input fails explicitly, with at most one fresh request retry. Single-only
warn-mode report attestation retains its existing unverified-result behavior.

Store commits compare captured lifecycle, binding and provenance inputs before
writing. QA commission and spawn intent are committed together, after a read-only
QA preparation and the existing exclusive claim. A late source race fails without
writing either. Spawn staging remains an external side effect: a race during that
stage is a post-staging failure, never authorization to launch a provider.

Error-alert reconciliation reads required notices, crash-recovery links, pending
rows and explicit folded/predecessor/proof dependencies through measured indexes.
Typed facts use the existing notification worker, decoder and partial key index;
reads are fresh after writes. Existing notification/voice retention and schedule
semantics are unchanged. Runtime/provider/platform validation is separate from
these synthetic source tests.
