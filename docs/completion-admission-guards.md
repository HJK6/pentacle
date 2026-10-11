# Lane completion and lead admission guards

U1 runtime completion enforcement is deferred pending resolution of spec-namespace freshness at commit. Lane `done` and current-generation open-lane lead closes do not read member criteria in this release. The U1 contract below is retained as the deferred design; its runtime enforcement and installed-release acceptance remain open. Parser/template behavior remains available for future integration. U2 spawn-WIP admission and the pre-existing close ladder, operator confirmation, actor/binding generation, CAS and replay checks remain active. No configuration switch enables U1 in this release.

The retained U1 design and active U2 check use the existing `PENTACLE_MEMORY_ROOT` work tree.
They neither edit specs nor activate policy, and add no store schema, service,
CLI option, reservation counter or fleet lock. A successful fixture run proves
source behavior, not installation or policy activation.

## Lane completion

A lane transition to `done` reads exactly its member specs. Closing a session
reads members of every open (`active`, `paused`, `blocked`) first-class lane
whose bound stream **and generation** identify that session as current lead.
Worker, QA, planner, helper, unbound, former-generation and done-lane closes do
not acquire a completion obligation from attached specs alone. Pausing and
re-leading a lane do not evaluate acceptance text.

Existing actor, generation, child-preservation, lane CAS and ownership rules
remain first. On session close, visibility and the idle/deferred decisions
already reached by that local or remote route remain first. A route that would
actually close, including either remote offline logical-close route, must then
pass completion. Existing liveness probes are not repeated.

Within `## Acceptance Criteria`, only the `### Custom` subsection is checked.
It ends at its next peer or parent heading; nested headings stay in scope.
Common and Closure criteria outside Custom are unaffected. ATX headings accept
ordinary indentation and closing hashes. Fenced code, inline code examples,
non-task prose and ordinary bullets are not completion criteria.

Ordinary unordered or ordered task checkboxes need no bold title or em dash:

```text
- [x] Completed outcome
- [ ] Remaining outcome (waived: a non-empty reason)
- [ ] Remaining outcome tracked: spec_alpha__follow (new_scope)
```

Checked state is evaluated first, including when the wording quotes disposition
syntax. An unchecked criterion needs one valid operative suffix at line end:
a nonempty waiver or a tracked target/class. Matched inline backtick spans and
paired ASCII or curly quotes are examples, not operative markers; apostrophes
inside words and escaped quotes do not begin/end quoted spans. An unmatched
quote does not hide a suffix. Multiple operative markers, empty reasons or
malformed suffixes leave a criterion undisposed. Empty unchecked criteria are
also retained as residuals. Formatting alone never causes a parse refusal.

Tracked criteria need class `external_event`, `operator_action` or `new_scope`,
a distinct resolver-proven follow-up, nonempty frontmatter `owner:`, and a live
receipt in that follow-up's `## Tracking` section:

```text
- deferred_from: spec_alpha__member — Remaining outcome: a concrete reason
```

Criterion identity uses a leading bold title when present; otherwise it uses
the full checkbox body before its operative disposition, with whitespace
normalized. Other literal Markdown is preserved. The receipt must identify
exactly one source identity before the reason delimiter. Duplicate identities
cannot grant tracked completion. Prefix collisions such as `Ship` and
`Ship: west` make a receipt ending `Ship: west: delayed` ambiguous; neither is
disposed by that receipt. Colon-bearing identities without a competing source
prefix and colon-bearing reasons remain valid. Code-example-only receipts and
bare deferred-with-reason notes grant nothing. No ID registry is introduced.

The follow-up must be in `backlog`, `analysis`, `ready_for_dev`, `in_progress`,
`needs_qa` or `blocked`; `completed` and `deprecated` are terminal. The returned
disposition records class, receipt reason and receiving owner. This is syntax
and linkage validation, not a judgment about whether deferral was appropriate.
Readable specs with no Custom section or no Custom checkboxes pass. Missing or
unreadable members fail closed; an unconfigured root is not a waiver.
A real affected lane remains open, and its current lead cannot close through
these paths until its owner restores the configured tree and readable member
spec at its correct identity, or corrects obsolete membership through the
existing authorized lane operation. The owner then disposes any remaining
Custom criteria and retries completion/close. A missing file cannot be cleared
by pretending it has an empty Custom section or by using a new bypass flag.

`CompletionRefusal` separates a stable code from its explanation and `items`:

- `lane_residuals_undisposed`: all undisposed member/criterion pairs in stable
  member/line order, including per-item `follow_up_not_found`,
  `follow_up_terminal`, `follow_up_is_self`, `class_invalid`,
  `follow_up_unlinked` or `follow_up_unowned` reasons; malformed operative
  suffixes use `disposition_invalid`, duplicate identities use
  `criterion_ambiguous`, and an ambiguous receipt adds `receipt_ambiguous` detail
- `member_spec_unreadable`: member identity and `retriable: true`

Refusal leaves lane state, version, events, publication and confirmation use
unchanged. A separate committed existing lifecycle audit records a refused
lane completion. A refused session close writes one existing close audit with
`fenced` disposition and does not terminate or close the session. The report
ledger retains an already durable report if its later requested close refuses.

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

From the repository root, with Python 3.13, service requirements, editable
`services/agent-orch`, and the public workflow's standard prerequisites:

```sh
export PYTHONPATH="$PWD/services:$PWD/services/agent-orch"
unset TMUX
export PENTACLE_SSH_CONTROL_DIR="$(mktemp -d /tmp/completion-ssh.XXXXXX)"
python -m pytest services/chat-stream-v2/tests/test_lane_close_residual_guard.py services/chat-stream-v2/tests/test_spawn_wip_cap.py -q -rs
(cd services/chat-stream-v2 && python -m pytest tests --ignore=tests/smoke --ignore=tests/soak -m 'not soak' -q -rs --collect-only)
(cd services/chat-stream-v2 && python -m pytest tests/smoke -q -rs --maxfail=1 --collect-only)
```

After committing the candidate, run the canonical serial daemon/CLI gate with
its evidence directory outside the checkout:

```sh
python services/chat-stream-v2/tools/source_integrity.py --root "$PWD"
python services/chat-stream-v2/tools/run_gate.py merge
python -m pytest services/agent-orch/tests
python services/chat-stream-v2/tools/source_integrity.py --root "$PWD"
```

The gate retains its own preflight, timeout, selectors and cleanup. Remove
only the owned temporary SSH directory after all test children finish. A
sandbox-denied required test remains authored/collected and assigned to fleet
intake with its exact error; it is not a pass or a reason to weaken assertions.
Both guards should be accepted together before operator-owned deployment.
