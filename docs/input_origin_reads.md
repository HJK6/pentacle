# Input-origin delivery reads

Session inventory and paged history still prove each USER or TELL origin from
its original dispatch or delivery evidence. A read never grants origin from
textual headers alone. Unproved TELL events retain the existing USER rewrite.

Those two read paths pass one `ProjectionDeliveries` object explicitly through
`_project_session_events_conn`, `input_origin`, and `retained_proof`. The
inventory holds its existing read savepoint; history pages hold a read savepoint
covering lifecycle resolution, event selection, and projection. Each object is
local to that call and is discarded on success or exception. It is never stored
on the Store or in a process cache. Write, promotion, single-event, and native
input-splitting callers continue to use current transaction data without it.

The object reads `tell_id,reply` once, lazily at the first delivery lookup. It
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

These checks exclude SQLite/Python disagreements in duplicate-key selection,
JSON extensions, scalar and collection extraction, NUL string handling, and
surrogate encoding. Valid escaped surrogate pairs decode to ordinary Unicode
and stay eligible. Unexpected parsing failures also disable reuse. When a row
or lookup falls outside the boundary, the current and subsequent lookups use
the original SQL and parsing, including its exceptions. There is no schema or
index change and no new proof condition.

## Regression evidence

`tests/test_read_projection_deliveries.py` drives real Store inventory and
history APIs. Its scale guard checks one delivery table read and exactly the
row population as message count doubles and unrelated deliveries grow. It also
checks six literal expected origins, projection freshness and exception
cleanup, and current-data promotion behavior.

The complete ordered origin logs and projected events are compared against
`tests/read_projection_legacy.py`, frozen from commit
`151067fe3591de95ae349f2edd1584df47e436eb`. Its SHA256 is
`29cc4e6c1f84051d28442d2dc094a41596569eda6fe824e7721198f30dfcef75`.
That oracle covers clean and unusual JSON, unrelated malformed rows, dispatches,
peer and service input, missing proof records, lifecycle mismatches, unsplit
quotations, overlapping deliveries, and own-row exclusion.
