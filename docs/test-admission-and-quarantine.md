# Test admission and quarantine

The blocking selectors use `scripts/quarantine.json` as the effective quarantine
list. A quarantine names one exact case. Python identities use the suite prefix
for new entries (`daemon::`, `cli::`, or `shared::`); the original unprefixed
daemon entries remain valid. JavaScript identities are a test file, `::`, and
the exact Node test name. The associated history records an activation identity
and, for nested Node tests, the parent test names. The selectors run those cases
visibly as advisory and preserve the remaining blocking outcomes.

A scheduled reconciliation consumes first-attempt scorecard classifications and
retained test-level receipts. It counts distinct reviewed no-code occurrences,
not retries or unclassified failures. It retains occurrences beyond the rolling
scorecard window, upserts the decision before proposing a selection change, and
prepares a one-file relay bundle with the fleet handoff check. A service
notification carries the decision, two evidence links, coverage loss, and the
bundle path and hash. Failed publication, relay, or notification remains
retryable. Public writes belong to the existing relay.

The daemon runs reconciliation when `PENTACLE_QUARANTINE_CONFIG` names an
absolute JSON configuration path. The configuration names `sources` (the
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

## Blocking-tier admission

The rollout inventory binds exact Python node IDs on their declared platforms and
JavaScript test files to their test and relevant fixture fingerprints. Its
source baseline is recorded in the inventory; the rollout receipt separately
binds the selector implementation. Python admission is per collected case.
The npm runner's admission is per test file. Separate gate entries that do not
use these selectors continue to block under their own contracts.

New or changed tests and tests restored after quarantine require an admission
attestation. The selector trusts attestations already present on the accepted
Git base. An attestation introduced by the current candidate is advisory until
its reviewed metadata publication lands and the next run observes it on the
base. A missing, stale, incomplete, or unreviewed record runs visibly as
advisory. An unreadable inventory makes the gate incomplete while known
blocking tests still run. Quarantine takes precedence over admission.

The attestation records the suite and exact Python ID or JavaScript file,
fingerprint, platforms, journey and boundary, failing-subject and
passing-control receipt references and SHA256 digests, typed independent QA
report ID, QA target commit, and any quarantine activation being restored.
Place the matching record fields in a versioned evidence manifest at
`scripts/test-admission-evidence/<sha256>.json`; the filename and attestation
must name the SHA256 of its exact bytes. A single manifest can cover several
cases. The reviewer checks the actual control receipts and uses that manifest
SHA256 as the typed report's gate-evidence digest. Publishing an attestation or
manifest does not change the test fingerprint.

Before relay of a bundle that adds or changes attestations, run the checker from
the accepted checker installation with the private work-report reader
credential available:

```
python3 scripts/admission_attestation_check.py --root <checkout> --base <base-sha> --tip <candidate-sha>
```

Exit `0` accepts the changed attestations, `1` refuses an invalid or rejected
attestation, and `2` reports unavailable Git or QA evidence. The checker reads
the exact reviewed target source, the evidence-manifest digest, and the
independently commissioned report. A refused or unavailable check cannot
publish an attestation.
