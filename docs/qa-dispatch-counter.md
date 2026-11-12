# QA dispatch reject counter

The daemon counts adjudicated valid QA rejects by spec, reviewed surface and
cycle. Two distinct valid rejects stop another commission on that surface until
an authorized adjudicator records a diagnosis and pivot. Changing a candidate
SHA does not reset the count. Two pending rejects require adjudication before another commission.

## Commission a review

A QA spawn creates a commission without requiring caller role, spec matching,
cycle or QA form fields. Supply a named surface when it is known. Otherwise the
daemon derives a `<spec_id>:<phase>` surface from the reviewer's or parent's
unambiguous spec and phase. That fallback has its own counter: omitting a locked
named surface can permit up to two more reviews before the fallback also locks.
If neither a surface nor a spec can be derived, the spawn is admitted with an
unknown-scope warning. QA handoffs continue the original commission.

A send is a commission when it carries explicit QA metadata; untagged sends are
ordinary messages. The daemon does not infer commissions from prose.

A commission binds the reviewer stream, its daemon generation, and message id
(spawn starts with message id 0), plus the coordinator generation and the
spec/surface/cycle. Reusing a send message id with different content or scope is a
conflict. Spawn transport retries retain the existing spawn-key replay contract.
A reopened stream has a new generation; its old reports cannot be rebound.
Send admission reads the open reviewer generation and canonical spec in the
commission transaction. Delivery checks that generation under the target lifecycle
lock; a subsequent reopen returns `qa_reviewer_generation_conflict` before input.

Scheduled QA retains its owner generation and is checked against the current
counter when it fires. Same-generation retries reuse the dispatch. A closed or
reopened owner cannot authorize that fire.

## Adjudicate and diagnose

Two open commissions with adjudicatable reject reports in the same
spec/surface/cycle refuse another commission with `qa_adjudication_required`,
listing the report ids and exact adjudication commands. Only reject reports with
the required typed evidence and an unambiguous commission binding count. Marking
a report `--void` removes its commission from the pending count; marking both
`--valid` moves them into the valid-reject count, producing
`qa_dispatch_reject_limit` until `spec-issue diagnose` records a diagnosis and
pivot. `diagnose` itself is never blocked by either limit and still requires two
valid rejects before advancing the cycle.

Only adjudicated valid rejects count toward the limit. Diagnosis still requires
two valid rejects and advances that surface's cycle after recording a pivot.

A QA report must be a durable `done` row with top-level `qa_verdict: "reject"`, a
full `target_sha`, and typed evidence in its existing extras object:

```json
{
  "qa_review": {
    "candidate_identity": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "reviewed_scope": "admission acceptance criteria",
    "gate_evidence_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
```

`candidate_identity` must equal `target_sha`. The digest is SHA-256 of the durable
gate/review evidence. The coordinator still judges whether the finding is an
in-scope substantive defect under Loop Containment; the daemon does not read
findings prose to invent validity.

The commissioned reviewer can construct that binding without writing
`candidate_identity` by hand. Supply the full reviewed commit SHA, the actual
reviewed scope and the SHA-256 of durable gate/review evidence:

```sh
agent-orch report --msg-id 7 --status done \
  --result '{"summary":"Reviewed admission","findings":[],"next_action":"lead_proceed"}' \
  --qa-verdict accept --target-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --qa-reviewed-scope 'admission acceptance criteria' \
  --qa-gate-evidence-digest bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --qa-commission-msg-id 7
```

The CLI copies `--target-sha` exactly into `extras.qa_review.candidate_identity`.
The two evidence flags require each other and an explicit full `--target-sha`;
blank scope, non-lowercase or non-64-hex digest, and a prebuilt
`extras.qa_review` alongside those flags fail before transport. Other `extras`
keys survive composition. `--result-file` follows the same rule and sends the
composed report inline. A valid prebuilt JSON `qa_review` remains usable without
the new flags. The reviewer still supplies the verdict and evidence digest;
the commissioned stream, generation and message id bind reviewer identity at
the daemon. This producer path applies to both ACCEPT and REJECT reports.

For a repair review, send a fresh typed commission with a new message id and
report against that id. `--qa-commission-msg-id` explicitly records that id in
`extras.qa_review.commission_msg_id`, even if the report itself uses `--msg-id 0`.
It requires the full review evidence flags and a nonnegative integer (0 names
the spawn commission). An explicit binding has no fallback: an absent, malformed,
wrong-generation or wrong-scope commission cannot consume another review.
Keep this identity in prebuilt `qa_review` JSON when not using the flags.

The daemon selects the current cycle for each new commission. A diagnosis after
two valid rejects advances the surface's cycle; changing a candidate SHA does not.

A verdict report without an explicit commission id filed at a different message id binds at adjudication to the
commission already holding its report id, else the exact-key commission, else
the reviewer's single open commission in that generation, else that generation's
only commission. Otherwise adjudication returns `qa_report_binding_mismatch`
with `candidate_msg_ids` listing the open candidate message ids in ascending
order. Retry, void and revalidate of a bound report keep its commission; the
stored report row, including its filed message id, is never rewritten.

```sh
agent-orch spec-issue adjudicate REPORT_ID --spec-id spec_pentacle__example \
  --surface admission --cycle 1 --valid --reason 'Acceptance criterion violated'
agent-orch spec-issue adjudicate REPORT_ID --spec-id spec_pentacle__example \
  --surface admission --cycle 1 --void --reason 'Finding withdrawn'
agent-orch spec-issue diagnose --spec-id spec_pentacle__example \
  --surface admission --cycle 1 --diagnosis-id pivot-20260912-1 \
  --diagnosis 'The original oracle omitted the generation boundary' \
  --pivot 'Repair the generation oracle and review the corrected scope'
```

Lead/Nexus mutations require an open verified caller with the canonical spec
binding and commission ownership: the original coordinator generation, its current
ancestor, or an explicit handoff successor chain. If the coordinator is unknown,
an otherwise authorized adjudicator may act. A planner additionally needs the spec
as its current assignment and may adjudicate only a `spec` phase commission.

The normalized phase is saved on each commission. Planner adjudication requires a
saved `spec` phase; diagnosis requires every counted reject to be `spec` phase and
owned under the same lineage rules. Changing the reviewer's phase cannot upgrade
an existing commission. Existing database rows retain a NULL phase and remain
available to lead/Nexus callers; planners cannot mutate those legacy commissions.
Mixed-phase rejects share the same counter and require lead/Nexus diagnosis.
Shared spec tags alone grant no planner assignment or commission ownership. The
reviewer cannot adjudicate its own report. These permissions grant no product
implementation authority.

The first adjudication permanently selects one representative report per
commission. Repeated adjudication is idempotent; a new report id for the same
commission returns `qa_commission_report_conflict` with the representative id,
even after that id is voided. Void/revalidate that original id; superseding report
ids are not supported. A fresh independent review needs a fresh commission.
Void excludes the representative; revalidation restores it in its original cycle.

Two active representatives cause `qa_dispatch_reject_limit`, including canonical
`spec_id`, `surface`, `cycle`, and ordered `report_ids`. Diagnosis requires two
valid rejects and nonempty diagnosis/pivot, records the historical ids, and
atomically advances the cycle. Identical diagnosis-id retries replay; changed
payloads conflict. Late adjudication in an old cycle never affects the new cycle.
`spec-issue show` exposes history; legacy `list`/`clear` do not clear this ledger.
