# Lane completion and lead admission guards

U1 completion enforcement checks a fresh preparation of member Custom criteria
for lane `done` and current-generation open-lane lead closes. Preparation runs
off the main/event-loop thread and off the Store worker. U2 spawn-WIP admission
and the existing authority, confirmation, lifecycle, CAS and replay checks stay
active.

Both checks use the existing `PENTACLE_MEMORY_ROOT` work tree. They neither edit
specs nor activate policy, and add no store schema, service, CLI option, identity
registry, reservation counter or fleet lock. A passing fixture run proves
source behavior, not installation or activation.

## Lane completion

A lane transition to `done` evaluates the Custom criteria of exactly its member
specs, plus required follow-up receipts; fresh identity resolution observes the
namespace. Closing a session evaluates members of every open (`active`, `paused`, `blocked`) first-class lane
whose bound stream **and generation** identify that session as current lead.
Worker, QA, planner, helper, unbound, former-generation and done-lane closes do
not acquire a completion obligation from attached specs alone. Pausing and
re-leading a lane do not evaluate acceptance text.

Existing actor, generation, child-preservation, lane CAS and ownership rules
remain first. On session close, visibility and the idle/deferred decisions
already reached by that local or remote route remain first. A route that would
actually close, including either remote offline logical-close route, must then
pass completion. Existing liveness probes are not repeated.

### Preparation and commit boundaries

Each completion request with member obligations has exactly one fresh namespace
preparation. It resolves identities, rejects ambiguity, parses only the consumed
members' criteria and reads required follow-ups. The preparation retains the
bytes, identity fingerprints and directory metadata it observed. This is not an
atomic filesystem snapshot. There is no hidden retry or second namespace scan.
An empty member set needs no filesystem scan.

For lane `done`, the short Store transaction preserves the existing actor and
binding generation, replay, conflict, lane-version and confirmation ordering.
It compares the exact prepared lane identity, state, version and members, and
checks metadata only for consumed member/follow-up files and captured directory
paths. It does not parse, walk or stat the whole namespace, or wait for an
off-thread scan. Producer, lane, action, answer and single-use confirmation
checks stay in that transaction; completion and confirmation use commit together.

Already missing, unreadable, ambiguous or unresolved input fails closed at
preparation. A detected consumed-input change before lane commit also refuses
with `member_spec_unreadable`, leaving the confirmation unspent. The caller can
restore the source and submit a new request. An already committed replay still
returns its original committed result, even if a new preparation finds a problem.

For session close, preparation and consumed-input revalidation are admission
checks under the existing lifecycle and graph fences, before any termination
attempt. Changes detected there refuse without terminating. The existing
terminate, confirm, then generation-fenced mark-closed ordering is unchanged.
Once that route has confirmed termination/death, a later consumed-source
freshness failure does not veto recording the same generation as closed. The
existing closed audit's reason instead includes `source_changed`, the target
generation and the source refusal. This flag is atomic with close persistence,
readable through `inspect_stream`'s `close_audit.reason`, and also returned on the
successful session as `completion_source_changed` and
`completion_source_warning`. It is a recorded limitation, not an undo or a fix
of the namespace gap below.

Only that late source check is advisory after confirmed death. Authority,
target-generation, child-preservation, idle, visibility and process-death checks
remain mandatory. Failed or uncertain termination never becomes confirmed death;
a replacement generation is never closed. A killed seat does not mark its lane
done. Offline operator/manager closes record deferred intent, not confirmed
death: their source guard is admission-only, with no post-admission source check
or source-changed flag. A later source edit therefore does not undo their admitted
intent; the existing target-generation CAS still applies. The existing close confirmation and replay behavior is unchanged; the
flag creates no new confirmation, retry, send or compensation mechanism.

Cancellation during preparation never enqueues completion. Cancellation after
lane enqueue is not proof of rollback: its outcome is unknown until the durable
result is read back, and no automatic retry is issued. Session finalization
continues once death is confirmed, including if the original caller cancels.
Before that irreversible point, cancellation while waiting to enqueue a logical
close cannot later cause a hidden close.

### Accepted remaining namespace gap

An unrelated existing spec's declared id can be rewritten in place to duplicate
a member's id after preparation, leaving the captured directory metadata
unchanged. Completion can then commit and spend the confirmation. The
consumed-input checks do not cover every identity-only file, and metadata checks
do not promise to detect every content edit. A later fresh check can refuse the
ambiguity but cannot undo the completion. There is no automatic undo. Ordinary
edits can reach this gap; there is no time-bound guarantee. The conformance test
expects this completion to succeed, then a fresh evaluation to refuse it. This
is an accepted limitation, not a fixed race.

### Criteria and refusal details

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
lane completion. A session source-admission refusal writes one existing close
audit with `fenced` disposition and does not terminate or close the session.
Late source drift after confirmed death is instead recorded on the closed audit
as described above. The report ledger retains an already durable report if its
later requested close refuses.

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

From the repository root, use one isolated Python 3.13 environment consistently
for the source-only completion checks. The `python3` below must be Python 3.13;
missing declared dependencies are environment errors, not skips:

```sh
python3 -m venv /tmp/pentacle-lane-guard-venv
/tmp/pentacle-lane-guard-venv/bin/python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
export PYTHONPATH="$PWD/services:$PWD/services/agent-orch"
env -u TMUX /tmp/pentacle-lane-guard-venv/bin/python -m pytest services/chat-stream-v2/tests/test_lane_close_residual_guard.py services/chat-stream-v2/tests/test_lane_completion_deferred.py services/chat-stream-v2/tests/test_work_lane_completion.py services/chat-stream-v2/tests/test_work_lane_progress.py services/chat-stream-v2/tests/test_assistant_composite.py -q -rs
/tmp/pentacle-lane-guard-venv/bin/python -m compileall -q services/_shared/custom_ac.py services/chat-stream-v2/store_work_lanes.py services/chat-stream-v2/sessions.py services/chat-stream-v2/assistant_composite.py services/chat-stream-v2/tests
```

These are direct synthetic pytest checks using invented specs and owned canonical
temporary roots. They run no tmux, Unix-socket or live-daemon tests and no gate
wrapper. Race cells verify the exact qualified callback, retain checked bytes
and fingerprints, and assert preparation, mutation, enqueue and callback order.
An early mutation or unmatched callback is `HARNESS_ERROR`.

Fleet runs at intake own the canonical merge gate on supported platforms, the
command-line, smoke and web gates, preserved spawn-admission controls,
independent final review of the exact candidate, and activation proof. A denied
required environment test stays authored/collected with its exact denial and
is labelled "fleet runs at intake"; it is not a pass or a reason to weaken an
assertion. A passing fixture run proves source behavior, not installation or
activation.
