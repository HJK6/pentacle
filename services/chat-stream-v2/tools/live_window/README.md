# Live window

`tools.live_window` is the only supported client-side path for creating and
tearing down harness seats against chat-stream-v2.  It performs the operator
nonce/proof handshake, records immutable spawn ownership (request ID,
idempotency key, stream ID, host/name, and session generation), and refuses to
send a close frame unless all bindings still match authoritative inventory.

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

The supported D2 acceptance caller is `tools/d2_live_delivery.py`. It requires
explicit release SHA, live PID, gate provenance and an external artifact directory;
it uses generation-CAS mode only and preserves at least 60 seconds of delivery
proof backoff. Run it once within the authorized release window. All independent
observations collect failures, including runtime-after, terminal episode stability,
4/4 durable cleanup, capability and foreign inventory, before the final verdict.
Mutating RPC errors stop the journey; library cleanup still runs in its context
manager. Foreign inventory is hashed before/after; equality is diagnostic only.
Acceptance requires no foreign target in the ownership-guarded helper mutation
audit or library cleanup receipts. Concurrent foreign inventory changes are allowed.

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
