# Daemon front-desk brief

`agent-orch assistant brief` reads a fresh daemon snapshot. The bound front desk,
current ruling advisor and operator credential may read it; other seats may not.
No model, provider CLI, checkpoint file, or UI is involved. This is advisory
state, not a durable delivery receipt or a substitute for owed-item evidence.

The fixed section order is header, owed items, operator questions, advisor
rulings, wakes, usage, windows, lanes, own children and commands. Each section
fails independently with `unavailable (<code>)`. Gatherers have a five-second
budget; database lists use SQL caps/counts except the existing question and
children APIs explicitly shared with their current callers. Lane summaries read
SQL-capped stored rows through the existing lane projection and generation-fenced
session presence, avoiding whole-board membership/report enrichment and lazy hue
writes. Up to 12 legacy paused/lead_lost rows receive canonical per-lane enrichment
to preserve their active owner-loss classification; a larger exceptional set is
reported unavailable rather than silently miscounted. Provider usage uses the
existing account readback API; no new provider or rollup calculation is added.

The pure composer takes a supplied UTC timestamp and reason. It does not read
a clock, database or file. It caps owed at 15, questions/rulings at 8 each,
wakes at five armed plus five ended, usage at one line per pool, windows at eight per
kind, lanes at 12 active/blocked and children at 10. Counts remain uncapped.
The footer uses existing CLI readers, plus an explicit read-only SQLite command
with a `<daemon-db>` placeholder for pending rulings, historical wakes and admission
freezes, which have no complete existing CLI list. It never selects kv data.
To fit 6000 UTF-8 bytes it removes individual item lines, from the end, in this
order: seats, lanes, windows, usage, wakes, rulings, questions, owed. Headers,
section counts and commands remain. Long item fields are whitespace-normalized
and clipped; omitted items can be read using the command footer.

## Delivery and predecessors

Guideline identity remains the hash of configured files only. A brief is gathered
only after delivery necessity and the existing 600-second repeat guard pass,
and appended to the same tell. Deferred compaction remembers its reason but
not a stale snapshot. Bind/rebind and same-stream generation restoration use
`bind`, `rebind`, and `restore`; compaction uses `compaction`; changed-file
refresh uses `requested` because the reason vocabulary has no file-change tag.
Daemon restart uses `daemon_start` and retains its original seat probes/header,
replacing the old checkpoint section with shared Owed/Wakes/Usage/Lanes.

The newest successful predecessor is selected from rebind audit, restore
history and committed transfer succession, comparing normalized timestamps.
No-op rebinds and failed/uncommitted transitions do not replace a predecessor.
Ended future wakes exclude explicit cancellation records and notes rearmed in
the current generation. They are informational, never silently rearmed.

## Advisory holds

`agent-orch hold acquire RESOURCE --reason TEXT --ttl SECONDS [--owner STREAM]`
requires the bound front desk or operator. TTL is required, positive and at most
86400 seconds. An existing unexpired unreleased hold conflicts, naming its ID
and owner. Owner defaults to the caller. A hold owner can release its own hold;
the front desk/operator needs `--force` for another owner's hold. Release accepts
a hold ID or resource. `agent-orch hold list [--resource RESOURCE]` is readable
by authenticated callers and filters expired/released rows. Holds neither enforce
admission nor run a sweeper. Fresh stores create the existing holds table shape
and its two indexes.

The bound front desk's context advisory says to finish the current step and run
`agent-orch compact`; a fresh brief follows. Other seats retain their checkpoint
instructions. This change does not edit role baselines, transfer rules, memory
files, or provider-code-identity inputs, and does not deploy or migrate a live store.
