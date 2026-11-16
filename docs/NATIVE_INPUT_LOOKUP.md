# Native input replay lookup

`tell_proof.split_native_input` retains an input's original segment boundaries
when intake replays it. The prior-copy lookup uses
`v2_native_input_lookup`, indexed by stream, session creation time and native
input identity, then checks the original text on the retained event row.
The normal prepared lookup reads only matching segments. Delivery, send-proof
and route lookups keep their existing behavior.

Startup creates empty lookup and coverage tables, an index, and three triggers.
It does not scan existing event JSON. Each `Store.prepare_native_input_events`
call allows about 200 ms for preparing existing rows across its inputs. Work
advances through the existing event-id index in pages of 128 rows, checking the
deadline between pages. This is a cooperative time budget, so an individual
page can finish after the deadline. The cursor and derived rows share a
savepoint. An enclosing caller transaction is never committed by preparation.
Coverage persists across Store and daemon restarts.

Until a seat is completely prepared, the original prior-copy query supplies
the answer. Inserts, updates and retention deletes maintain the lookup through
SQLite triggers, including writes from older binaries after rollback. A lookup
joins back to `session_event_tail` and rechecks the original scope, identity and
text, so a stale derived entry cannot supply an answer. If any mechanism table,
index or trigger needs recreation, startup atomically resets all coverage.
The next bounded preparation repairs that seat's derived entries.

Every segment normally carries the same `native_segment_spans`. The prepared
lookup chooses the greatest event id when all matching cuts agree. If old
matching rows disagree in cuts, it uses the original query and its original
row choice. Removing one sibling segment therefore preserves the retained
boundaries; removing every sibling allows ordinary first-sight processing.

Malformed event JSON is the one deliberate behavior difference: this lookup
skips it instead of raising. Triggers guard the typed identity read with
`json_valid`, so malformed inserts and updates still succeed. No textual
prefilter is used: it would need to cover escaped property names and, in
measurements, scanning a large string cost more than SQLite's reused JSON
parse. Only string native identities enter the derived index.

Rollback uses the previous binary without a schema downgrade. Added schema
remains compatible with older writers. No transcript offsets are persisted,
and no event content, segmentation, provenance or identity is rewritten.

`tests/test_native_input_lookup.py` compares emitted fields against the frozen
pre-change splitter in `tests/native_input_legacy.py`. It covers first sight,
replay, incomplete preparation, retention, lifecycle separation, conflicting
cuts, schema repair, malformed writes, transaction rollback and restart.
Instruction-count assertions detect renewed per-input history scans without
depending on machine timing. Existing intake and provenance journey tests
exercise the shared Store entry used by local and satellite ingest.
