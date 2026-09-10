# Capability dashboard boundary

The desktop may retain a generic capability dashboard, but a public build must show only data supplied by a reviewed adapter. If the adapter is absent, the dashboard reports `unavailable` instead of reconstructing private project state.

## Public row contract

Rows may contain a stable id, repository label, topic, status, owner-neutral display label, progress summary, and next action. Synthetic rows with unresolved metadata belong in an `Unresolved` section. Unknown statuses belong in an `Other` column so a typo is visible.

## Renderer behavior

Client-side filters may reapply repository, host label, status, and search text to an already-fetched batch. All text is escaped. A primary action is one of `unresolved`, `drive`, or `spawn_additional`; the renderer does not infer authority from a row.

## Capability states

The adapter reports `healthy`, `degraded`, or `disabled` with a bounded explanatory message. A disabled or degraded subsystem does not expose a private watcher, shared-memory path, remote service, or deployment command.

Use synthetic rows and `coordinator` through `satellite` labels in tests. The exact data source and request names belong to the public daemon contract when that adapter is implemented.
