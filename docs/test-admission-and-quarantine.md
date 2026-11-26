# Test admission and quarantine

The blocking selectors use the candidate's `scripts/quarantine-v2.json` as the
single effective list. Each row has an exact `id`, `owner`, `reason`, and ISO
expiry date; an expired row fails the gate. Adding a row takes effect in that
change, and removing it immediately restores blocking. Each blocking case lost
by quarantine or deletion also needs a `Coverage-Drop: <exact ID>` commit
trailer. Python IDs include the suite prefix (`daemon::`, `cli::`, or `shared::`).
The retained v1 file at `scripts/quarantine.json` serves the installed legacy
reader until its next release. New proposals update only the v2 file.
The active list is for Python cases. Quarantined cases run as advisory and cannot
change the blocking verdict. JavaScript cases use native skips in source; a skip
of a formerly blocking case also requires a coverage-loss trailer. The automatic
worker reports exact JavaScript case identities for review without proposing a
list row. Nested names use a compact JSON array of ordered name components.

A scheduled reconciliation consumes first-attempt scorecard classifications and
retained test-level receipts. It counts distinct reviewed no-code occurrences,
not retries or unclassified failures. It retains occurrences beyond the rolling
scorecard window, upserts the decision before proposing a selection change, and
prepares a one-file relay bundle with the fleet handoff check. A service
notification carries the decision, two evidence links, coverage loss, and the
bundle path and hash. Failed publication, relay, or notification remains
retryable. Public writes belong to the existing relay.

A scheduler outside the chat daemon runs reconciliation as
`python -m agent_orch.quarantine_run --config <path>` with an absolute JSON
configuration path, under a bounded run timeout, and sends the generic
`flaky-test-quarantine` notice when a run fails, times out or cannot start. The configuration names `sources` (the
scorecard source map), `memory_root`, `owner`, `epic`, `machine`, `checker`,
`packet_root`, `ledger`, and `state`. Paths must be absolute. `checker` is the
approved fleet-mode guard, and `packet_root`, `ledger`, and `state` are private
runtime locations outside the source tree. `agent_orch` optionally names the
installed CLI. The worker is bounded and its state is serialized; the notifier
uses the service producer and does not inherit a seat identity.

For recurrence after a repair, configure `restorations_file` as an absolute
path to a JSON object keyed by test ID. Each value names `manifest`, `sha256`,
`report_id`, and `at`. The private manifest binds the independent repair QA,
merged target, installed blocking gate and hosted passing JUnit. A recurrence
uses only later occurrences; a missing or invalid proof raises an alert. To
reopen a terminal decision, configure `requesting_stream_id` with the
publisher's authorized host and seat identity. The original item and its
observations remain linked to the new activation.
