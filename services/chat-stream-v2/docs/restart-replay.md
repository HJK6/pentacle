# First transcript replay

Intake still starts at byte zero and normalizes every record. On first binding
it captures the last complete record boundary. Until that boundary is consumed,
it can omit work for an exactly retained event whose processing has no writes.
There is no persisted cursor, schema change, or new wire request.

`replay_noop.stored_prefix` checks the open lifecycle and the insert's unique
identity key, including `session_created_at`. A missing identity is a fallback,
never evidence that a later event covers an earlier one. The read-only check
holds at most 128 events and a bounded receipt projection inside one store call.
It stops before uncertain work and after an observation that can initiate a
routing notification. No dependency view survives that call.

Receipt rows are append-only. The check groups proof metadata by receipt ID:
a bound row settles earlier unbound rows for that same receipt. Changed wires,
duplicate JSON keys, malformed data, excessive rows, and possible pending wire
matches take the ordinary path. This applies to output text too, since the
existing reconciliation routine can encounter quoted input in output events.

Inputs always run the original splitter. During replay only, its delivery scan
may read the existing connection delivery lookup, retaining that lookup's SQL
fallback for unclean data and its existing invalidation behavior. Splitting,
receipt stamping, and tell promotion remain authoritative. After splitting and
stamping, a retained segment without possible receipt or tell work can omit its
duplicate append/reconciliation. Partial inputs, changed delivery cuts, and
unbound proofs therefore still reach the original routines in source order.
Each event retains its own write transaction.

Activity restoration, usage/provenance, context crossings, tuple observation,
normalizer state, lifecycle checks, and broadcasts for newly inserted events
remain on the normal path. Already-retained events do not become new activity
or broadcast again. Skipping an ignored insert can remove gaps in future
AUTOINCREMENT values; identities, ordering, and references remain unchanged.
New IDs must still exceed every ID assigned before replay. Event-page cursors,
submission-proof watermarks, the native-input index, and retention use ordered
comparisons rather than the size of an ID gap.

While a replay makes progress through complete unread records, the pump yields
with `sleep(0)` between its normal bounded round-robin passes. Byte/event caps
and per-stream backoff remain in force. An incomplete line or a failed attempt
does not cause a busy loop. After the captured boundary, the normal cadence and
processing path apply.

One `ingest_replay_complete` log record names the stream, generation, captured
boundary, consumed offset, elapsed time from daemon startup, and disposition.
`consumed` establishes completion; `rotated` is an explicit disposition rather
than a successful replay claim. A restart measurement compares these records
against the independently captured open-seat inventory and transcript boundaries;
missing records or missing post-start events never count as success. Production
measurements also bind the daemon's startup timestamp and installed candidate.

Regression coverage compares the ordinary path with replay for both providers,
missing start/middle/end events, partial inputs, changed split dependencies,
unbound receipt/tell crash gaps, generation replacement, rotation, truncation,
and return to steady state. Copy-based differential runs use controlled clocks
and remap generated event IDs/references by identity and rank; performance runs
do not claim raw wall-clock-field equality.
