# Sidebar Visibility

The desktop sidebar is daemon-owned. Its authoritative inventory is the
chat-stream websocket snapshot followed by `session.inventory` frames;
`list_sessions` is legacy compatibility and never populates sidebar rows.
Direct tmux access is reserved for terminal operations and never fills rows,
including during cold start or a degraded websocket connection.

## Visibility Invariant

The desktop subscribes with `include_subagents:true`, so every projected row
carries the daemon's own visibility metadata. Rendering is a positive filter:
only rows whose `visibility` is exactly `"default"` are admitted. `nested`,
`hidden`, missing, and unknown values fail closed.

The decision does not use a prior-inventory join, session-name prefixes, or a
raw-tmux fallback. A status-only disconnect frame preserves the last daemon
inventory; a later explicit inventory replaces it, including removing absent
rows.

## Regression Coverage

`test/sidebar_filter.test.js` covers direct hidden/nested exclusion, missing
visibility fail-closed behavior, and degraded-to-reconnected inventory
replacement. `test/e2e/scenarios/desktop_hidden_visibility_degraded.js`
captures the real Electron sidebar after its isolated scripted daemon is
stopped and proves that a live hidden tmux session is not rendered.
