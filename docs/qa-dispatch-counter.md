# QA dispatch reject counter

The v2 daemon tracks coordinator-adjudicated QA rejects by canonical spec, stable
acceptance surface, and cycle. Two valid rejects from different commissions stop
new QA commissions on that surface until a lead or Nexus records a diagnosis and
pivot. Candidate SHA changes do not reset the count. This is source support for a
coordinated rollout; enforcement defaults off.

## Commission a review

Choose a stable lowercase surface slug (letters, digits, `.`, `_`, `-`, maximum
80 characters). Keep it for the same acceptance surface across candidate SHAs.
Renaming a surface is an explicit coordinator scope decision, not a reset shortcut.
Read the current cycle with:

```sh
agent-orch spec-issue show --spec-id spec_pentacle__example --surface admission
agent-orch spawn --provider codex --model gpt-5.6-sol --effort xhigh \
  --role qa --objective 'Review admission' --spec-id spec_pentacle__example \
  --qa-spec-id spec_pentacle__example --qa-surface admission --qa-cycle 1
agent-orch send HOST:QA_STREAM 7 'Review the repaired admission surface' \
  --qa-spec-id spec_pentacle__example --qa-surface admission --qa-cycle 1
```

`role=qa` or `phase=qa` is recognized case-insensitively. A send to a durable QA
seat is a QA commission; explicit QA fields also mark sends to other roles.
Enforcement requires all three QA fields. There is no prose classification.
Ordinary workers and immediate or scheduled handoffs remain exempt. Use a worker
for repair execution; a review remains QA even if its prompt calls it a repair.

A commission binds the reviewer stream, its daemon generation, and message id
(spawn starts with message id 0), plus the coordinator generation and the
spec/surface/cycle. Reusing a send message id with different content or scope is a
conflict. Spawn transport retries retain the existing spawn-key replay contract.
A reopened stream has a new generation; its old reports cannot be rebound.
Send admission reads the open reviewer generation and canonical spec in the
commission transaction. Delivery checks that generation under the target lifecycle
lock; a subsequent reopen returns `qa_reviewer_generation_conflict` before input.

Scheduled QA with `--at` or `--delay` persists the same fields and owner generation.
Insertion linearizes eligibility at its check before writing the schedule row;
it reserves no future review. Fire checks
current count/cycle again before creating a pane. A diagnosis in between makes the
scheduled cycle stale; create a new schedule with the new expected cycle. Same
schedule-generation retries replay the existing dispatch; rescheduling to a new
generation is a new admission. A reopened/closed owner cannot authorize that fire.

## Adjudicate and diagnose

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

Mutations require an open verified lead/Nexus with the canonical spec binding and
commission ownership: the original coordinator generation, its current ancestor,
or an explicit handoff successor chain. Shared spec tags alone grant no authority.
The reviewer cannot adjudicate its own report.

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

## Activation and rollback

`PENTACLE_QA_DISPATCH_MODE=off` is the default and the admission kill switch.
Legacy QA without metadata remains compatible while off. Explicit metadata still
validates and records commissions; spec-issue history remains available. Set
`enforce` only in the coordinated daemon release after callers carry the new
fields. Legacy unscoped immediate/scheduled QA then returns `qa_scope_required`.
An invalid mode returns `qa_dispatch_mode_invalid` for QA; it does not freeze
unrelated workers. No daemon restart or deployment is performed by this feature.

The integration owner records each installed artifact, live PID, activation
readback, and rollback preimage. Source tests establish counter/admission behavior;
they do not establish fleet activation. The new `v2_qa_*` tables and nullable
schedule fields are additive and survive restart or switching enforcement off.
