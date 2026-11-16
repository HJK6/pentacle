# Input-origin delivery reads

Session inventory and paged history still prove each USER or TELL origin from
its original dispatch or delivery evidence. A read never grants origin from
textual headers alone. Unproved TELL events retain the existing USER rewrite.

Those two read paths pass one `ProjectionDeliveries` object explicitly through
`_project_session_events_conn`, `input_origin`, and `retained_proof`. The
inventory holds its existing read savepoint; history pages hold a read savepoint
covering lifecycle resolution, event selection, and projection. One delivery
lookup is retained on each Store connection and shared by those two readers.
Write, promotion, single-event, and native input-splitting callers continue to
use current transaction data without it.

Connection-local TEMP triggers mark the lookup dirty on delivery inserts,
updates and deletes, including replacement inserts and rolled-back writes.
Each reader also checks `PRAGMA main.data_version` inside its read savepoint:
any other connection's main-database commit rebuilds the lookup, including an
unrelated commit. Unrelated writes on the Store connection do not rebuild it.
An existing outer transaction gets a private lookup, so uncommitted data never
enters the shared table. Each new connection installs its own triggers after
schema setup commits. A failed trigger installation rolls back the complete
installation; an invalidation callback fault disables sharing. Both fall back
to private tables for the connection's remaining lifetime and warn once.

Only the delivery lookup persists. `ProjectionReuse` retains tail windows
within one call, guarded by `total_changes`, and is released on success or
exception. Receipt proofs use the original per-input filtered SQL query.
Neither receipt rows nor finished history answers are cached.

The object reads `tell_id,reply` and SQLite’s extracted delivery target and text
once, lazily at the first delivery lookup. It
parses rows once, groups candidates by exact target and text, groups overlap
checks by target, and retains each delivery text digest. An authenticated
operator dispatch returns before that lookup and does not build the table.
Existing proof anchors, check order, ambiguity rules, and early returns remain
unchanged. Notice projection keeps its separate indexed delivery lookup.

## Compatibility boundary

Reuse applies only when every row meets the following boundary:

- The reply is text containing a standard JSON object. Every nested object has
  unique keys. Nonstandard constants, comments, trailing commas, and other
  extended JSON syntax are rejected by the strict parser.
- `delivery` is absent, null, or an object. Other shapes, including falsey
  scalars and lists, fall back to the original path.
- Delivery target and text are absent, null, or strings containing neither NUL
  nor an unpaired surrogate. The same string rule applies to lookup arguments.

The same build statement extracts the two lookup fields with SQLite
`json_extract`. Each extracted value must equal Python’s parsed value in both
type and value, and must be null or a string. This runtime agreement check is
additional to the guards above. It catches differences such as NUL inside an
object key aliasing a normal key in SQLite. Valid escaped surrogate pairs
decode to ordinary Unicode and stay eligible when both parsers agree.

Every row is validated before either lookup map is exposed. A mismatch on a
later row invalidates the entire build. SQL, fetch, or parse failures disable
reuse; the build cursor closes before fallback. When a row or lookup falls
outside the boundary, the current and subsequent lookups use the original SQL
and parsing for their own answers or exceptions. This fallback is sticky for
the shared object's lifetime, until invalidation causes a fresh build. Proof
conditions are unchanged. Both notice projection reads use the additive index
on `v2_outbound_notices(recipient_stream_id)`; old code can ignore this index
after a source rollback.

## Regression evidence

`tests/test_read_projection_deliveries.py` drives real Store inventory and
history APIs. Its scale guard checks one delivery table read and exactly the
row population as message count doubles and unrelated deliveries grow. It also
checks seven literal expected origins, projection freshness and exception
cleanup, and current-data promotion behavior.

`tests/test_history_delivery_lookup.py` compares shared and freshly constructed
tables after seeded local and external mutations, malformed rows, rollback and
outer transactions. Disabling either local triggers or the data-version check
must break equality. It also checks commit-during-read snapshot isolation,
sticky fallback, failure handling, trigger lifetime and both notice query plans.

`tools/benchmark_history_lookup.py` runs the real Store thread on an owned
extract, with eight-event pages, lookahead and cursors. It measures no writes,
unrelated local commits between calls, and delivery commits between calls.
Each case has one warm-up and five measured walks. Timings exclude queue wait
and include the whole Store callable, matching `store_call_slow`. The tool
mutates its database: supply only a private owned extract with its paired
archive and capture manifest. Extract measurements are not live acceptance.

The complete ordered origin logs and projected events are compared against
`tests/read_projection_legacy.py`, frozen from commit
`151067fe3591de95ae349f2edd1584df47e436eb`. Its SHA256 is
`29cc4e6c1f84051d28442d2dc094a41596569eda6fe824e7721198f30dfcef75`.
That oracle covers clean and unusual JSON, unrelated malformed rows, dispatches,
peer and service input, missing proof records, lifecycle mismatches, unsplit
quotations, overlapping deliveries, own-row exclusion, and NUL-key aliases
before and after ordinary rows. The alias case independently expects unknown.
