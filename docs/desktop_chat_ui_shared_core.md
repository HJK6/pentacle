# Desktop chat UI shared core

The desktop chat surface is organized around a small shared state contract. The renderer owns DOM presentation and host integration; a pure store owns normalized session state, transcript rows, optimistic sends, and reconnect reconciliation.

## Boundaries

| Layer | Public responsibility |
|---|---|
| transport adapter | opens the websocket and forwards typed frames |
| store controller | applies snapshots/events and exposes selectors |
| view helpers | derive display rows, status labels, and question payloads |
| renderer | mounts HTML, handles user input, and emits accessibility state |
| host adapter | supplies configuration, theme, and IPC seams |

The store must not read the DOM or a private filesystem. The renderer must not reach into the daemon's database. This separation makes reducer tests deterministic and lets a fixture transport drive the same UI as a real websocket.

## State model

The public state contains:

- session summaries keyed by stream id;
- transcript rows in oldest-to-newest order;
- working and connection status;
- question payloads and draft answers;
- optimistic sends keyed by stable optimistic id and request id; and
- bounded diagnostics suitable for local tests.

Runtime text is treated as untrusted input. HTML helpers escape text, links accept only `http` or `https`, and attachments carry metadata rather than file contents.

## Send lifecycle

An optimistic send starts as `queued`, becomes `dispatched` after the transport accepts it, and ends as `acked`, `echoed`, or `failed`. A reconnect may replay only explicitly retry-safe survivors with the original request id. A daemon restart marks ambiguous sends indeterminate so the user can choose whether to retry. An explicit retry creates a new request id.

## Transcript rendering

The view receives a bounded window of rows and renders content-bearing DOM elements. A paint proof should assert the row kind and a bounded text prefix, not merely that a transport event arrived. Question controls are generated from the parsed payload, use accessible labels, and submit one deterministic answer object.

The latest user row derives a receipt caption from its correlated durable echo (see the send-receipt design note): a committed-but-unconfirmed receipt reads Sent once the echo has bound, never sticking at Sending. An attachment send's server echo is the daemon's agent-facing wrapper text (`Look at the image file at <path>, then respond…`), not the operator caption; the reducer correlates that wrapper to the optimistic row by its embedded attachment keys and keeps the operator caption as the rendered text across the live, snapshot, and duplicate-replay reconcile paths, so one caption bubble renders rather than a second wrapper row.

## Status and navigation

The sidebar orders rows by open question, working state, recent activity, and stable stream id. Status cards render only fields supplied by the daemon. Missing optional fields produce no placeholder claims. The status view exposes a return path to the transcript and a separate update-history region.

## Public adapter contract

The shared core may be supplied by a package or by a small reviewed adapter. The desktop entrypoint should depend only on the published reducer, selector, and question-formatting interfaces. A fixture adapter may implement the same functions with synthetic sessions so the renderer can be developed without a private submodule or remote service.
