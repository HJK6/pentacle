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

## Mobile parity in desktop and web

Both hosts use `renderer/src/shared_transcript_view.ts`. Tool output and subagent
messages honor the core disclosure preview and expose the full escaped body
through keyboard-accessible expansion. Expansion follows the stream and row
identity across refreshes. File actions retain their body, and code activity and
box diagrams preserve whitespace. The shared outer parser's blank-paragraph
split inside fenced code remains a known limitation in both clients.

Images precede captions; image-only messages have no empty bubble or text-copy
button. History fetches and snapshots retain user attachments even when their
caption is empty. Recognized Option-B answer text displays question labels, answers and
notes while copying the original message. Hidden events and composer drafts
remain outside the transcript.

Delivery captions consume the core receipt; failure and cancellation override a
stale caption. The renderer never infers Sent from elapsed time or an RPC result.
Normal assistant text uses Rajdhani at 14.5px/22px and user text uses JetBrains
Mono at 13px/20px. Desktop compact density remains available at 12px/18px.

## Question completion and recovery

The question overlay lists pane children in their original order followed by
open durable notifications in creation/id order. Each page retains its draft by
source identity. Selection bounds apply, custom text replaces selected options
and their note, and Send answers requires every unlocked page to be valid.
Cancel and Escape close the overlay locally. Composer text cannot bypass a
multi-page or mixed-source group.

A complete pane group produces one `question.dismiss` call containing all child
answers. Each durable notification uses the existing `notification.resolve`
bridge. Successful identities remain locked during partial failures. If a
resolution reply is lost, Check answer status reconciles `prompt.list` before
resubmission; reconnect also retries that reconciliation. Existing stale-pane
and text-send-failure recovery retains the serialized answer in the composer.

Desktop supports the public v2 single question per durable notification and
multiple simultaneous notifications. Nested durable child-question protocols
are not assumed. Unsupported entries cannot silently settle a parent.

Resolved durable answers hydrate through `prompt.list` with `open:false`, so an
answer is visible even without a transcript echo. The view inserts it by immutable
resolution time, and a core answer row with the same notification identity takes
precedence. Consumption timestamps do not move old answers later in the chat.
