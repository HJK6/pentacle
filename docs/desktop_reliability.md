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

## Chat history states

Chat popouts bind fetched history by their explicit stream id even when a closed
stream is absent from the live roster. Roster metadata still supplies live status
when available; its absence does not imply an open or working session. Assistant
direct popouts retain their source and generation validation.

`renderer/chat_events_lazy.js` tracks history requests independently from
websocket connectivity. Failed fetches and completed fetches that leave no
transcript rows retry after 1, 2, 4, 8 and 16 seconds while the stream remains in
an active chat slot and the host is connected. Recovery shows “Loading
messages…”, including above durable answered-question groups. Cached rows and
the draft remain visible while syncing, recovering or reconnecting.

After the retry budget, a successful empty fetch shows “No messages yet.” and
a failed fetch shows “Messages could not be loaded.” Both offer Retry, which
starts a fresh budget. See [Lazy history recovery](desktop_chat_ui_shared_core.md#lazy-history-recovery).

Each attempt has a distinct identity. Leaving the stream's chat slots,
disconnecting or replacing the load invalidates stale retry work; a successful
load with rows ends recovery. Reconnect fetches active chat streams again. A
late result cannot overwrite a new attempt. Successful zero-row replies still
notify the renderer, so bounded recovery continues even without an event frame
to repaint the view. Transport loss does not imply turn completion or message
delivery.
