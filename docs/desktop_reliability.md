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

Attached chat-mode streams use the shared cache's existing focused pin, so
traffic from other streams cannot evict their loaded transcript. Leaving chat
mode or detaching the last pane releases the pin; inactive streams remain
subject to the weighted cache budget. Detaching also invalidates the stream's
lazy-load marker, so reopening fetches authoritative history again. A durable
answer group or a synthetic last-message summary alone does not count as loaded
message history, even when answers exceed the visible window. An ordinary row
elsewhere in retained history counts as loaded without widening the painted window.

A long-lived web page checks the served build on reconnect and at the existing
ten-minute polling interval. A different known build rehydrates the open chat
slots through the reconnect snapshot/backfill path once per observed build.
Concurrent reconnect/update recovery shares one resync; a failed state read
retries on the next check. Unsent composer text and image attachments stay in
the page. The refresh icon remains available to load new client code.

The isolated browser regression is `test/e2e/web_chat_history_retention_gate.cjs`:
run it after `npm run build:web` with:

```sh
PENTACLE_TEST_BROWSER=/path/to/chrome node test/e2e/web_chat_history_retention_gate.cjs /temporary/evidence
```

The hermetic CLI `test/e2e/web_gate.js` includes this check
in the existing Public checks workflow. It uses a loopback fixture daemon and
actual Chrome/CDP, forces reconnect and unrelated-stream cache pressure, checks
reopen/reload, and advertises changed builds to the old page while preserving
an unsent draft/image. Its retry probe fails one state read. JSON/screenshots,
asset/config/harness hashes and cleanup receipts bind the result. No live
provider or operator stream is contacted. The ten-minute poll is accelerated
only in the harness, identically for baseline and candidate.
