# Store work on ingestion and scope reads

Daemon notice projection strips provider-supplied `raw.daemon_notice` on every
input. It looks up proofs only for unbound USER events whose text begins with
`[pentacle-notice:` and whose stream and positive integer event ID are valid.
These are necessary conditions of the existing final proof check. Exact body,
generation, delivery and ambiguity checks still decide authority. Kind matching
uses the existing `str(kind or "").upper()` behavior; client bindings retain
existing handling of booleans, strings and other values.

Asset ownership and question visibility read retained lineage through
`Store.list_session_lineage()`. One ordered LEFT JOIN returns stream ID, parent,
handoff predecessor, visibility and generation for open and closed sessions.
It preserves session table order and represents a missing generation as an
empty string. Scope traversal, generation-bound roots, handoffs, nearest visible
ancestors, cycle handling and the existing 64-visited-node ancestry bound are
unchanged. Full session reads retain their existing hydration contract.

## Measurement limits

An owned synthetic baseline with 2,163 retained sessions measured median Store
execution of 36.815 ms for asset scope and 39.056 ms for question lineage. An
actual scope operation took 39.342 ms while an append queued during it waited
39.272 ms. This establishes a workload-specific queue contribution from that
Store operation. With 10,000 synthetic notices, an ineligible event's unnecessary
notice scan took 0.779 ms median. The repair eliminates SQL for ineligible inputs;
this is not a measured workload-wide improvement or a user-decode benchmark.

With the same harness, generated input and Python runtime, candidate median
Store execution was 1.946 ms for asset scope and 2.016 ms for question lineage.
The ineligible projection at 10,000 notices took 0.001 ms. All three met the
predeclared decision bound of at most half their baseline. The candidate queue
witness overlapped a real 2.071 ms scope read; the append waited 2.019 ms and
then inserted durably. No artificial Store delay was added.

Installed acceptance remains pending. These measurements
do not establish exclusive attribution of the historical 85-second ingestion
delay or current production event rates. Native event preparation time is not
the durable commit time. No scheduler, concurrency, polling, schema or index
change is included.
