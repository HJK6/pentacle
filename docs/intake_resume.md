# Intake restart positions

Claude and Codex intake can resume from a persisted complete-line position.
Cursor and Gemini retain full replay: their continuation formats are not covered
by this checkpoint version. Set `PENTACLE_INGEST_FORCE_FULL_REPLAY=1` to reread
from byte zero for every provider.

The first start without a checkpoint still replays the transcript. A later
restart verifies the entire saved prefix with SHA-256 on a worker thread and
resumes after it. This verification reads bytes; it does not normalize the
prefix or repeat its event/accounting/receipt work. Existing native-session and
current source-identity checks still apply. Verification time grows with prefix
size; it is not a constant-time seek.

## Saved state and completeness

`v2_ingest_positions` is an additive table with one row per stream. Its small
position document contains the explicit format version, provider, generation,
lifecycle, native session, path, file identity, byte offset and consumed-prefix
digest. Separate documents hold provenance continuation and the generation-bound
last Claude routing tuple. An unchanged continuation document is not rewritten.
Idle passes do not write a checkpoint.

Activity, preview, routing context, routing episodes and usage projections are
rebuilt from stored data by the existing startup path. A checkpoint never
replaces these with an older snapshot. Failure/backoff and inventory throttling
are also fresh after restart.

The checkpoint is published only after the consumed bytes finish event append,
send-receipt binding, tell promotion, accounting, provenance, routing/context
observation and publication effects. Capped passes conservatively publish no
position. A caught best-effort failure holds advancement for the rest of that
stream binding, even if later passes succeed. The earlier complete position
remains usable; the next restart reprocesses everything after it. A failed
checkpoint transaction cannot advance the saved comparison cache.

Carried provenance context always matches the consumed offset. A capped Codex
pass retains context from its consumed records only; a pass that does not
advance restores its starting context, including Claude's cap path. This fixes
an existing attribution error where unread model metadata could affect an
earlier usage record on the next pass. Context is copied and recomputed in
memory without another Store call. Whole-span passes retain their existing
effects, and capped passes still publish no checkpoint.

The encoded record is limited to 256 KiB. Oversized continuation is not trimmed;
it disables the saved position and requires full replay. The format version is
changed deliberately when state shape or meaning changes. A source-code release
alone does not invalidate positions. The shape fixture in
`test_ingest_checkpoint.py` pins the serialized contract.

## Trust and concurrent file changes

Missing, unreadable, unknown-version, malformed or oversized records fall back
to full replay. Provider, generation, lifecycle, native-session, path and file
identity must match. The saved offset must end a complete line, the file must
not be shorter, and the full consumed-prefix digest must match. Current source
authority is checked independently of the saved record.

The proof is of the bytes **as read**, not an atomic file snapshot. Appending
while verification runs is allowed. A rewrite before the verifier reads a block
causes a hash mismatch. A rewrite behind the verifier can escape that restart,
just as a rewrite behind a running tailer does. The live digest retains the
bytes actually read, including subsequent consumed suffixes, so the next
restart detects that rewrite. Modification time is not a separate trust rule.

An arbitrarily old checkpoint is supported, including one left behind by an
older binary. That binary can continue using the additive schema. Returning to
new code verifies the old prefix and processes the entire remaining suffix;
there is no rollback trigger, epoch table or source-file fingerprint.

## Equality and recovery

The reference is the uninterrupted run with the same pass boundaries, including
a boundary at the saved position. Normalization can already depend on those
boundaries. Clean stops preserve ordered event pushes, provider timestamps,
stored events, receipt proof, promotion, accounting and continuation.

A process kill also disconnects clients. After an append-then-kill cut, the
event remains readable through `request_stream_events` on reconnect. Replaying
that durable event repairs unfinished proof/promotion without pushing it again.
Events first stored after restart produce ordered `chat.event` frames normally.
Inventory transport throttling starts fresh; its rebuilt projection is compared
separately from event pushes.

Three historical full-replay effects are deliberately absent before a proven
position: re-observing old routing tuples, incidental repair of older incomplete
work, and normalizing new records together with earlier records across the saved
pass boundary. Forced full replay remains available for those historical repairs.

## Diagnostics and validation

`ingest_position` records the resume decision and fallback reason.
`ingest_prefix_verified` records bytes, hash duration and MB/s.
`ingest_position_held` records the first reason a binding stops advancing.
`ingest_replay_complete` includes the resume reason and counts quiet resumed
seats as well as seats that processed new events. Debug-level
`ingest_position_saved` reports encoded changed-column bytes and wall duration;
these bytes are logical payload, not physical WAL traffic.

Focused tests use separate processes, reopened file-backed Stores, real client
history reads and SIGKILL at processing/commit boundaries. SQLite guards confine
their connections and attachments to owned fixtures and reject `kv` reads.
The predecessor-source rehearsal uses the actual prior revision when that Git
object is available; a shallow checkout without it reports a skip, not a pass.

Performance acceptance uses explicitly inventoried, relocated database/archive
copies and frozen transcripts. Never open an unrelocated preimage with Store:
its archive path can still point at the running daemon's archive. Local tests
and copied-data timings do not prove deployment; runtime acceptance requires
the second restart with the code, a controlled message and the subsequent
latency observation.
