# Live window

`tools.live_window` is the only supported client-side path for creating and
tearing down harness seats against chat-stream-v2.  It performs the operator
nonce/proof handshake, records immutable spawn ownership (request ID,
idempotency key, stream ID, host/name, and session generation), and refuses to
send a close frame unless all bindings still match authoritative inventory.

The handshake makes at most one fresh-socket re-proof, within the original
timeout, and only when the hello was sent before the welcome's advertised
`expires_at` and its `hello.error` arrived at or after it (or carries
`code: operator_nonce_expired`): the server judged a timely hello after it had
queued. A late send, a denial before expiry or a second failure raises
`LiveWindowError` with the first failure retained in
`handshake_diagnostics`. Both timing samples are wall clock in the same domain
as `expires_at`, which holds for the same-host loopback restoration and smoke
callers; a cross-host caller is still bounded to one retry. Each failed socket
is aborted without waiting for any close handshake (websockets `close_socket`,
which also ends the receiver), a re-proof transport failure still reports the
first failure, and no application request is ever retried. One absolute
deadline covers both attempts: every step uses only the remaining budget, and
a welcome, error or snapshot that arrives at or after it is never acted on.
The deadline is rechecked after proof construction, and a peer that has begun
closing gets no hello. Each proof socket is opened with `close_timeout=0`, so
no send or close waits for a closing peer. Only after a timely snapshot, with
the deadline and an open, non-closing state rechecked under the protocol lock,
does the connection get its normal close timeout; a connection the peer has
begun closing fails the handshake instead.

The authenticated snapshot must advertise
`capabilities.close_expected_generation=true` before normal activation. That
wire capability is forwarded to the runtime's lifecycle lock, so a close
captured for an old same-name generation cannot kill a replacement. A runtime
that omits it is refused by default. The only fallback is an explicit
`allow_legacy_close` caller flag and a never-before-used session name ending in
the activation idempotency UUID prefix; its receipt records the residual
non-atomic legacy race. This is for an already-pinned legacy runtime only, not
an alternative normal path.

Use `LiveWindow` when the harness can inspect the daemon's session database and
the private tmux server: it requires both a durable `sessions.status=closed`
row and physical tmux absence before reporting cleanup.  Use
`OwnedSessionRegistry` only when a caller needs its own authenticated event
reader, as fleet smoke does; it still owns registration, close-frame creation,
generation fencing, retry-close, and checkpoint recovery.

New callers must not build `hello`, `close`, or rescue-close frames themselves.
They must preserve the state checkpoint and bind receipts with candidate SHA,
runtime PID, helper hash, evidence paths, close-capability state, and the
durable closed-row/physical-tmux predicates through `write_receipt`.

Satellite discovery is a required boundary for remote seats. Its default
`v2-*` admission covers `v2-fleet-smoke-{provider}-{uuid_prefix}` while its
legacy smoke exception permits only eight hexadecimal characters. Keep the
12-character ownership suffix and use the admitted `v2-` prefix. The caller
regression test passes generated names through the real satellite discovery
method. Follow-up: move this validation into the library's name-construction
boundary so every remote caller validates satellite admission before spawning;
never shorten the ownership suffix to satisfy the legacy smoke exception.

Fleet smoke records client convergence (the existing two-second bound) separately from provider terminal idle (the cell timeout). A marker does not imply an idle provider. D2 compares durable delivered_at with controlled command start/end timestamps; the 60-second proof backoff observes durability and does not define delivery time. For helper-only repairs on a held runtime, --candidate-sha identifies the executed helper checkout and --runtime-sha identifies the frozen runtime; receipts retain both.

Read-only SQLite polls close the connection on both success and query failure. The SQLite transaction context alone does not release the connection; relying on garbage collection can exhaust a low file-descriptor budget during long acceptance or teardown polling.
