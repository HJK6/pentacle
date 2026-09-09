# Desktop reliability guardrails

The desktop should remain useful when one transport or optional feature is unavailable. Reliability behavior is expressed as local, testable state transitions rather than a particular deployment topology.

## Connection isolation

Terminal attachment and structured chat use separate adapters. A healthy websocket does not prove that a terminal can attach, and a healthy terminal does not prove that the chat daemon is reachable. Each adapter reports its own state and error.

## Reconnect behavior

On reconnect, the client waits for the server greeting, sends `hello`, applies the authoritative snapshot, and then reconciles optimistic rows. Requests are correlated by id. Only retry-safe sends from the current socket generation are replayed; ambiguous rows after a daemon restart remain visible for explicit user action.

## Bounded resources

Transcript windows, diagnostics, log lines, and evidence payloads are bounded. File logging rotates before the configured byte cap, truncates oversized lines at UTF-8 boundaries, and never lets a console pipe failure create a logging loop.

## Degraded UI

The sidebar may show a disconnected or stale state while preserving the last safe local view. Missing optional status fields do not become fabricated values. A failed microphone or dashboard adapter must not block chat or terminal operation.

## Testing

Exercise reconnect, delayed replies, duplicate frames, daemon restart, log rotation, and one failing optional adapter with synthetic clocks and loopback fixtures. Tests should run in any checkout using temporary stores. They must not assume a named workstation, a private SSH route, or a live operator session.
