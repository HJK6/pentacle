# Repository activity metadata

The repository activity registry records collaboration presence, not ownership or completion. A repository can be visible from multiple worktrees and clients; a registration never grants access to a session or changes project data.

## Identity and privacy

Git remotes normalize to a logical `forge/owner/repo` id. A repository without a remote uses an explicit local id. A worktree path is local metadata and should not be returned in broad metrics. Path-bearing responses require the caller's verified ownership context.

The client probes only read-only Git metadata: remote, branch, HEAD, and upstream. Identity is derived from those facts rather than from a caller-provided display name.

## Lifecycle

```text
register → active → refresh (only when material facts change) → release
                    └──────────────────────────────────────→ stale
```

Rows and compact lifecycle events may be retained in a local database. A no-op refresh does not advance a revision. Closing a session marks its registrations inactive. History filters immutable event metadata, while the current row contains the latest snapshot.

## Generic commands

```text
agent-orch repo register <path> [--local-id <stable-id>]
agent-orch repo refresh <path>
agent-orch repo release <registration-id>
agent-orch repo list
agent-orch repo history <repo-id>
agent-orch repo metrics
```

All paths must be explicit and local to the configured adapter. The public registry does not expose a managed-host route, production rollback behavior, transcripts, diffs, commands, or secrets.

## Scoped awareness

An awareness adapter may publish normalized `repo:<id>` and `machine:<label>` tokens with a monotonic revision. It emits a delta only when the effective token set changes. Install providers before clearing stale sources and rebuild persisted state without replaying lifecycle events.
