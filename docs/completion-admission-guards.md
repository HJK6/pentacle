# Lead WIP admission and lifecycle completion

Lane completion and session close do not read spec Markdown, parse acceptance
criteria or follow-up receipts, or fence on source-file fingerprints. Spec
and lane tracking remain available; agents and owners are responsible for
recording honest completion and unfinished work in their specs.

Authenticated lifecycle authority, generation identity, lane version/CAS,
ownership, idle/safety checks, child/report obligations and transactional
lifecycle state remain enforced. Mandatory work-lane membership is not a
spawn prerequisite. Ordinary spawn authorization, attribution and
idempotency remain enforced.

## Lead admission

The optional policy file is `work/wip_policy.json`:

```json
{"schema_version":1,"cap":8,"snapshot_at":"2026-01-01","grandfather":{"alpha":["alpha__inherited"]}}
```

This is synthetic example data. Policy creation and edits belong to the
operator. No file means disabled, silently. A present policy must have schema
version integer 1, a positive integer cap (not a boolean/string), a real
`YYYY-MM-DD` date, and a repository-to-list-of-strings grandfather mapping.
Invalid or unreadable policy refuses with exact message `wip_policy_malformed`.

For repository R, identified by the folder prefix before `__`:

- P is the set of stems in `work/in_progress/`
- A lead candidate X is included once regardless of its current folder
- G is the listed grandfather set intersected with this candidate-inclusive P
- O is the subset with exactly one valid override in `## Tracking`
- Effective additional work is `E_R = |P minus (G union O)|`

```text
- wip_override: 2026-01-01 fixture-ref — a non-empty reason
```

Only live declarations within a Tracking heading count. Fenced examples and
ordered-list prose grant nothing. Nested Tracking subsections remain in scope
until the next peer or parent heading; Markdown closing hashes and heading
indentation follow the policy parser. Override declarations use the exact
line format above, without leading indentation or extra field separators.

The override date must be real, its authority token nonempty, and its reason
nonempty. Authority references are parsed, not externally verified. Duplicate
or malformed declarations refuse with
`wip_override_malformed: spec=<canonical-spec-id>` and no computed count.
A spec read or identity scan failure refuses with
`wip_spec_unreadable: spec=<spec-id> exception=<exception-class>` and no
computed count. The exception class is reported without its message. A scan
failure names the candidate; a peer read failure names that peer.
Grandfather/override overlap is exempt once and can never make E_R negative.
Stale grandfather stems produce the structured `grandfather_stale` warning and
do not lower the count. Other repositories do not contribute to R's count.

A non-exempt candidate refuses only when its inclusive count is greater than
the cap; equality passes. An exempt candidate passes even if other items keep
the repository over cap. Already-active candidates count once, so moving a
candidate before spawning does not evade the check. A grandfathered item can
be re-led; an ordinary over-cap item needs its one valid override.

The shared formatter emits exactly:

```text
wip_cap_exceeded: repo=alpha E_R=9 cap=8 G_R=0 O_R=0
Add to this spec's Tracking section: - wip_override: YYYY-MM-DD <authority-ref> — <reason>
```

Only a new admission with resolved role `lead` and a primary resolved spec is
checked, before any new reservation, intent or pane. Other roles and unbound
leads are unaffected, including malformed-policy trees. Recorded same-key
replay and payload conflicts retain precedence over policy. Expired empty
reservations do not confer replay eligibility; retained intent/pane records
and terminal records retain existing replay behavior. New concurrent keyed
admissions still use the existing atomic claim.

These are tree checks, not cumulative admission accounting: no new distributed
reservation, history lookup or lock is introduced.

## Conformance and validation

`services/chat-stream-v2/tests/fixtures/wip_cap_conformance.json` is schema-1
JSON data. Its 54 named rows specify explicit active stems, policy, override
text, candidate location and exact count/code/message/warning expectations.
Tests materialize each tree in temporary directories and assert both the
shared evaluator and the real spawn admission path with synthetic transports.
The same data is suitable for a separately owned memory-validator adapter;
that adapter and policy activation are outside this change.

Run `services/chat-stream-v2/tests/test_spawn_wip_cap.py` for the retained
WIP policy, `test_enforcement_retirement.py` for removal acceptance, and
the canonical `services/chat-stream-v2/tools/run_gate.py merge` gate
before release. Source checks do not establish runtime activation.
