# Receipt reads and first transcript replay

Intake starts at byte zero and processes every record. Event identity,
transaction boundaries, broadcasts, activity, usage, routing and normalizer
state stay unchanged. An already-bound receipt skips the read-only proof
calculation whose result would be discarded; unbound receipt repair and tell
promotion still perform their original proof checks.

Receipt proof reads use a partial expression index over the destination stream
and input-proof wire text. SQLite can evaluate the substring filter from the
index without parsing every receipt's metadata for every event. The predicate
and expression are shared with `send_proofs` so its query matches the index.
Both use `CASE WHEN json_valid(...)` to keep malformed receipt metadata writable.
The intended behavior change is that proof reads skip malformed metadata;
previously SQLite raised when encountering it. Valid metadata retains the same
ordering, limit, proof selection and later-bound-row precedence.

A second guarded expression index covers delivery destination and wire text.
The input splitter retains its destination filter and substring match; the
notice lookup during reconciliation retains exact destination/text matching.
These readers skip malformed reply JSON while inserts and updates still accept
it. Other delivery readers retain their existing behavior. In particular,
promotion's normalized-digest ambiguity check, the input-origin fallback SQL,
and the shared delivery lookup are unchanged. Split spans keep their original
ordering and overlap checks; equal notice candidates retain rowid order.

The hot indexes are created at Store startup. Fresh archive pairs get the receipt
index during initialization; existing pairs get it through the validated archive
migration after the hot transaction commits. Pair identity and exact schema
checks still precede archive changes. Missing, busy, incompatible or foreign
archives retain their existing historical-read behavior. Historical attachments
remain read-only. These additive indexes may remain after a source rollback.

At first binding intake captures the transcript's last complete record boundary.
While replay makes progress through complete unread records, the pump yields
with `sleep(0)` between its bounded round-robin passes. Byte and event caps,
per-stream backoff and steady-state cadence remain in force. Incomplete records
and unsuccessful attempts do not create an immediate retry loop. No read
position survives a restart.

One `ingest_replay_complete` log record names the stream, generation, captured
boundary, consumed offset, seconds since daemon startup, and disposition.
`consumed` establishes completion; `rotated` is an explicit disposition. A restart
measurement compares these records with an independently captured inventory of
open seats and transcript boundaries. Missing seats or post-start events cannot
count as success; closed and unavailable transcripts need explicit dispositions.
There is no new wire verb or persisted replay state.

Regression coverage includes hot and archive query plans, malformed writes,
archive upgrades, both transcript providers, missing start/middle/end events,
partial inputs, changed split dependencies, receipt and tell crash gaps,
generation replacement, rotation, truncation and return to steady state.
Real-copy equality uses fixed logical clocks, compares new event IDs by rank
and maps references by identity. New IDs must exceed every pre-restart ID.
Client cursors, send-proof watermarks, native-input lookups and retention depend
on ordering, not on the size of numeric gaps.

Copied harnesses must relocate the archive path before Store startup and reject
SQLite connections or attachments outside their evidence directory. They also
reject every read of `kv`. The authorized empty capacity-retry projection in
that harness is an explicit equality exclusion; full gates and runtime use the
real capacity state.
