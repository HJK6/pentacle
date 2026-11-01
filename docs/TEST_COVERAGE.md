# Test coverage by behavior

Start with the behavior below, then follow its existing tests and runner. Paths
stay in place because discovery, test-to-test imports and selection depend on
them. This is a navigation map and bounded inspection, not a coverage percentage
or a certification of every assertion or installed client/provider combination.

[E2E harness](E2E_HARNESS.md) owns environment setup and gates;
[test runner](TEST_RUNNER.md) owns npm discovery and cleanup;
[capabilities](TEST_CAPABILITIES.md) owns capability/skip classification.
[Test selection](TEST_SELECTION.md) and [selective release](SELECTIVE_RELEASE.md)
own advisory change selection. CI, merge and deployment gates remain required.

## Advisor admission cases

All cases below are in `services/chat-stream-v2/tests/test_assistant_lane_rulings.py`.
They run in the daemon unit and focused `v2-pytest` selections; delivery uses
real Comms and Store with synthetic seats and fake pane transport.

| Test | Behavior |
|---|---|
| `test_covered_fd_spawn_of_any_non_qa_role_waits_for_approval` | Planner and untyped covered spawns wait for approval. |
| `test_fd_qa_commission_lead_owned_worker_and_handoff_are_not_held` | QA, lead-owned and handoff controls retain ordinary admission. |
| `test_fd_resume_is_not_held` | A front-desk resume is not held. |
| `test_covered_fd_spawn_without_spec_is_refused_work_assignment_required` | New covered spawn requires a work assignment. |
| `test_covered_fd_send_waits_and_deny_delivers_nothing` | Send approval lands once; denial and absent assignment do not deliver. |
| `test_typed_qa_send_tell_and_non_fd_send_are_not_held` | Typed QA, tell and non-front-desk controls retain ordinary behavior. |
| `test_fd_self_send_and_send_to_protected_assistant_seat_are_not_held` | Self-addressed and protected-assistant reports retain ordinary delivery. |
| `test_covered_request_stays_pending_past_deadline_and_accepts_late_approval` | Overdue work remains held and accepts a late ruling. |
| `test_covered_request_holds_when_authority_unavailable_or_unconfigured` | Covered work holds across authority loss or missing binding. |
| `test_pending_covered_request_follows_rebind_and_old_authority_cannot_rule` | Rebinding retargets notices and fences former authority generations. |
| `test_explicit_disable_releases_covered_pending_and_mirrors_once` | Explicit disable releases and mirrors once. |
| `test_same_key_identical_digest_reuses_row_and_changed_digest_is_refused` | Retry identity and changed intent are distinguished for spawn and send. |
| `test_completed_same_key_send_returns_recorded_outcome_after_assignment_removal` | Completed retry remains idempotent after assignment removal and disable. |
| `test_completed_same_key_spawn_returns_recorded_outcome_after_assignment_removal` | A completed spawn retry does not recheck a removed assignment. |
| `test_approved_send_replay_returns_recorded_unproven_outcome` | Approved retries preserve the durable unproven receipt outcome. |
| `test_noncovered_original_authority_can_still_deny_after_rebind` | An ordinary close remains bound to its original authority after a rebind. |
| `test_final_send_notice_exposes_delivery_and_receipt` | Final notices retain the send delivery and receipt identity. |
| `test_covered_send_payload_is_normalized_before_digest_packet_and_release` | Body aliases and ordered attachment descriptors remain consistent. |
| `test_covered_send_deny_is_accepted_when_target_generation_is_stale` | Stale targets can be denied but cannot be approved. |
| `test_covered_column_is_added_to_an_existing_database_once` | Legacy and fresh stores have the additive column. |
| `test_approved_send_release_has_one_logical_identity_and_truthful_delivery_state` | Receipt reconciliation distinguishes landed, not landed and unproven delivery. |
| `test_covered_notice_carries_work_item_origin_and_send_context` | Packet distinguishes operator origin and agent goal, including missing origin. |
| `test_non_covered_actions_keep_deadline_release` | Non-covered close retains deadline behavior. |
| `test_unrelated_approved_work_continues_while_another_is_pending` | An unrelated pending ruling does not block an approved request. |

## Working testing standard

A good test protects intended behavior with enough realism to catch a meaningful regression and no more machinery than that protection needs. Keep existing good tests; this standard guides new/changed tests and gradual improvements.

| Property | What to establish |
|---|---|
| **Locatable and clear** | Identify the behavior/failure protected, locate its test in the existing structure or flow map, and find its intended invocation. Confirm that runner includes it, or document a deliberate opt-in runner and its limits. |
| **Effective** | Assertions observe the promised result or invariant and would reject a meaningful wrong outcome. Expected values are independent of the implementation logic being checked. Several assertions may protect one coherent scenario. |
| **Controlled and isolated** | Establish relevant state/order deliberately; bound completion; control data and outbound effects; release owned resources on every exit. Wait on an observable condition where available. A bounded absence-observation window or real-clock test may be justified, with its reason, tolerance and limits stated. |
| **Right-sized real boundary** | Keep the behavior under examination real and control unrelated dependencies. State what the test does not prove. Cases, parameter dimensions and expensive setup should protect distinct risks; similar-looking tests may exercise different boundaries. |

A shared flow map improves navigation; its unfinished state is organization work, not by itself a defect in each test. For a negative side-effect claim, establish how the test would detect the prohibited effect at its chosen boundary, or state the narrower proof. A missing spy alone does not prove a gap: inspect guards and existing failure paths.

Clock reads do not automatically justify sleeps: inspect existing clock/patching seams and ordering tolerances before judging them necessary or defective. A polling observation of process snapshots has blind spots; an acceptable bounded design is not proof that every prohibited call is observable. Deliberate opt-in means documented intended coverage, not exemption from required release checks.

### Assess with evidence

For each named scenario/property, record **supported**, **specific shortcoming**, **unknown**, or **not applicable**, with a short reason and source citation. Record evidence kind separately: **static**, **retained-run**, or **controlled-red**. Supported/static means the inspected structure supports the stated property; it is not a runtime pass or a flake-rate claim. A known static defect must name the failure mechanism. Uninspected tests remain unassessed.

Retained execution counts only after checking the actual result and its source/harness/scope identity; an existing log or JUnit file alone is insufficient. Controlled-red includes a verified applied regression and its expected assertion failure, with restored GREEN. Use that proof when oracle strength is uncertain or materially rewritten; do not require a mutation for every test.

Label findings as **weak test**, **coverage gap within the inspected scope**, **organization debt**, **candidate duplication**, or **necessary complexity**. Missing evidence is not itself a test failure. Similar names, elapsed duration and case counts do not establish bloat. Report measured cost at its actual scope, including setup and grouped subcases where relevant; do not compare unlike nodes as equivalent work. No summed score, file grade or suite-wide compliance percentage from a sample.

### Preserve and refine

Assess the reference flow with the same rubric as other flows; it need not fail to validate the rubric. Preserve unique assertions/failure modes when changing tests. Test-to-test imports are fixture dependencies: inspect all importers before a move or shared-helper change. Use focused checks while repairing and existing final-candidate gates. Classify product, harness and cleanup failures before changing an oracle.

Existing focused daemon runs already include capability/assertion/skip guards. Their assertion and skip preservation checks cover the tests named in the H2 manifests; they are not a suite-wide semantic-preservation guarantee. Keep their current behavior and scope.

Refine a rule when a concrete counterexample demonstrates ambiguity, unnecessary complexity or missing protection, even if only one flow exposes it. Preserve versions and recheck affected assessment rows after a meaning change. Prefer improving or retaining an existing example over adding a parallel suite.

### Adoption

Use this initially as guidance inside existing reviews and gates for new or materially changed tests. An unrelated legacy deficiency does not block a scoped change. Do not discard coverage, weaken assertions, add skips, enlarge timeouts or restructure the whole suite to claim compliance. No mandatory per-test form or universal fixture/test-layer template.

Keep one repo-local documentation home, linked to existing runner/selection authorities. Broader blocking enforcement or new automated checks require a separate reviewed proposal defining the objective rule, scope and demonstrated benefit. This version adds no gate, registry, service, approval hop or change to existing release requirements.

### Assessment scope and examples

A static assessment at `1e3c3241445fcdfa4b88575c18082f8632d16cab` inspected
22 named scenarios, rather than the whole suite. Client reconciliation, CLI
payload and owned-cleanup examples have positive static support. Its rotated
receipt and forced-overlap shortcomings are addressed by the reference below;
they are historical findings, not additional open repairs. Process snapshots
can miss brief prohibited descendants. Late-proof no-resend discrimination,
ordering in the sampled disconnect case, some clock/cleanup questions and required-gate inclusion
of standalone client tests remain unproved. The source-bound assessment is
retained with the planning evidence; it supplies neither runtime reliability
proof nor a suite compliance score. Remaining findings stay with the existing
work owner for bounded, change-relevant follow-up.

## Reading the map

**Inspected** means the named assertion/call path was read. **UNKNOWN** means
assertions or invocation have not been established in the stated inspection.
**GAP** requires a specific expected behavior and inspected absence within a
bounded scope. Candidate **BLOAT** needs demonstrated overlap and cost; similar
names or repeated state checks alone do not justify removal. No test here is
proposed for removal, and no candidate BLOAT is established by this tranche.

The structural snapshot is public source
`2fc273a8b5fcc23af1634ea3e481bcbcbc03f41a` (2026-10-31). Discovery reads every
tracked root/nested `package.json`, pytest configuration,
[`gate-env.json`](../scripts/gate-env.json),
[`test-selection.json`](../scripts/test-selection.json) and
[`Public checks`](../.github/workflows/predeploy-tests.yml), then reconciles
literal paths/globs, runner imports and the browser scenario registry. The
source-pinned census and method are retained with the implementation evidence;
they are not a second maintained registry. Reproduction uses `git ls-tree -r`
and `git archive` at that SHA, static Python ASTs and local JS import/path
resolution, without importing or executing subject code.
Behavior examples combine the planning inspection with the reference-flow
implementation; structural discovery does not audit the other assertions.

The filename population is `test_*.py`, `*.test/spec.{js,ts,tsx,mjs,cjs}`,
`test/e2e/scenarios/*.js`, E2E `*_gate.py`, and the two pushguard shell proofs.
Additional tracked JS/TS/Python/shell candidates have a test directory or
test/gate/smoke/soak/scenario/proof/harness/replay/fixture/runner filename.
Each path has one primary census entry; cross-flow links below do not add counts.
Helpers, fixtures, runner files and production dependencies are separated.
Literal references can be conditional or fixture strings and do not prove
runtime invocation. Computed imports/commands, environment-selected profiles,
pytest-generated nodes, optional dependencies and manual scripts outside this
filename scope remain UNKNOWN; no exhaustive manual-script or assertion audit
is claimed.

Filename hints leave 250 primary candidates behaviorally unclassified, retained
explicitly in the census. The behavior table inspects examples within families;
it does not relabel that unknown population as semantically covered.

## Runner reachability and structural exceptions

The snapshot has **896 primary candidate files** and **174 additional script
candidates**. Following runner/configuration references adds 200 dependency
files: the reconciled union is 1,270 distinct paths, partitioned as 896 primary
candidates, 13 runners, 77 helper/fixture candidates, 84 additional candidates
with unresolved role, and 200 dependency boundaries. These are file counts,
not functions, parameter cells or executed nodes. Relative to planning source
`1e3c3241`, the new candidates are `test/renderer_settings_chat_width.test.js`
and `test/e2e/web_chat_width_gate.cjs`. Neither changes the reference flow.

| Primary population (disjoint paths) | Files | Actual entry and limits |
| --- | ---: | --- |
| `services/chat-stream-v2/tests/`, excluding smoke/soak | 466 | [`run_gate.py`](../services/chat-stream-v2/tools/run_gate.py) unit selects `tests`, ignores smoke/soak and excludes `soak`/`repin_exhaustive`. Markers, plugins and capabilities still affect nodes. |
| Daemon `tests/smoke/`; `tests/soak/` | 8; 3 | Same runner: smoke explicitly selects its directory; soak is opt-in with `-m soak`. Merge is unit + smoke, not soak or exhaustive repin. |
| `services/agent-orch/tests/` | 81 | `cli-pytest --select tests`; separate Public checks contracts partition. |
| `services/_shared/tests/` | 8 | `shared-pytest --select tests`; separate from daemon tiers. |
| Root `test/` | 205 | Root npm shallow globs select 203 JS/TS tests. `test/test_specs_parser.py` and `test/test_voice_reply.py` are outside those globs; their focused commands are documented in [public boundary](public_boundary.md) and [room microphone conversation](room_mic_conversation.md), respectively. These explicit entries do not establish required-CI inclusion. |
| `test/e2e/terminal_interaction_runtime/` | 3 | Its `*.test.js` files join root npm tests; `npm run test:terminal-interactions` separately launches the owned Electron harness. |
| `test/e2e/`; `test/e2e/lib/`; `test/e2e/scenarios/` | 7; 7; 31 | Outside root npm shallow globs. Explicit npm selections can run nested tests. Default browser invocation uses the registry below, not a recursive scenario-directory glob. |
| `pentacle-chat-core/tests/` | 44 | Public checks frontend runs `npm --prefix pentacle-chat-core test`: TS tests plus `typecheck:test`. `typeContracts.ts` is typecheck support, not an executed `.test.ts` file. |
| `mic-server/test/` | 24 | Public checks contracts explicitly runs `PYTHONPATH=mic-server python3 -m pytest mic-server/test`. |
| `scripts/` | 6 | CI explicitly runs `test_check_public_residue.py` and `test_ci_required_aggregate.py`; `test_gate_env.py`, `test_select_tests.py` and two pushguard shell proofs have separate entries, not blanket CI inclusion. |
| Other | 2 | The filename candidates `services/_shared/test_resource_ledger.py` and `services/chat-stream-v2/tools/test_attempt_metrics.py` are support modules, not standalone test suites. The former is imported by CLI conftest/tests; the latter is tested by daemon `tests/test_test_attempt_metrics.py` and used by `tools/usage_rollup.py`. Historical filename counts remain unchanged. |
| `vendor/dashboard-library/tests/` | 1 | Vendor `npm test`, explicitly invoked by Public checks frontend. |

[`gate_env.py`](../scripts/gate_env.py) maps the closed target list and provisions
the declared dependencies. Daemon `v2-pytest` adds four capability invariants to
every requested selection; it does not substitute for the full merge gate.
`v2-merge-capacity` retains unit 1200s / smoke 900s; CI uses its separately
declared `v2-merge-ci` profile. The shell smoke/soak wrappers call the same
`run_gate.py`; their existence does not add tests to merge.

[`gate_web.py`](../scripts/gate_web.py) invokes
[`web_gate.js`](../test/e2e/web_gate.js) and the separate interrupt test.
The browser gate uses the 39 registered names in
[`lib/web_scenarios.js`](../test/e2e/lib/web_scenarios.js); imported scenario
helpers are support/registered behavior, not independent test nodes. It also
uses `lib/seed_web_gate.py` and `lib/web_gate_daemon.py`. After a successful
unfiltered run with its owned profile, it additionally invokes
`web_chat_history_retention_gate.cjs`, `web_chat_history_paging_gate.cjs` and
`web_default_pane_gate.cjs`. External profiles and scenario filters bypass those
three follow-ons. `test/e2e/scenarios/*.js` is a separate legacy population;
matching names alone do not establish default execution.

Other E2E entries such as `grid_split_gate.js`, `send_refusal_gate.cjs`,
`web_host_recovery_gate.cjs`, `web_assistant_direct_entry_gate.cjs`,
`web_chat_history_transport_gate.cjs`, `web_popouts_gate.cjs`,
`web_sidebar_resize_gate.cjs`, `web_chat_width_gate.cjs`,
`own_assistant_bootstrap_gate.py` and `report_selection_copy_gate.py` are
separately invocable. No default browser-wrapper invocation was established for
them. Their fixture/assets directories, `configs/`, `file_delivery/`, daemon
`tests/cosmo_e2e/` support and conftests are not extra passing tests. In the
reconciliation, 427 paths have no established top-level selection rule;
that includes support/dependency files, not 427 missing tests.

## Find a behavior

Patterns below are navigation hints within the structural population, not claims
that every matching file has been semantically audited. Unless stated otherwise,
assertion completeness, uninspected fake boundaries and runtime cost are UNKNOWN.

| Behavior / purpose | Existing protection and reachability | Boundaries, inspected protection and remaining uncertainty |
| --- | --- | --- |
| Advisor new-work admission | [`test_assistant_lane_rulings.py`](../services/chat-stream-v2/tests/test_assistant_lane_rulings.py); daemon unit / focused `v2-pytest` | Covered spawn and send admission, late ruling, authority loss and rebind, identity and normalized payload, stale targets, migration, and interrupted receipt reconciliation. Uses real Store, Sessions and Comms with fake pane transport; live provider and deployed readbacks remain separate. |
| Send retry → durable receipt | [`test_send_idempotency_dedupe.py`](../services/chat-stream-v2/tests/test_send_idempotency_dedupe.py), [`test_send_semantics.py`](../services/chat-stream-v2/tests/test_send_semantics.py); daemon unit / focused `v2-pytest` | Real Comms, Store, Sessions, SpawnCtl; fake tmux/hosts/blob transport. Inspected: single paste, logical identity, failed retry eligibility, queued and pending replay. Reference below; no real provider/socket/browser proof. |
| Receipt projection and late proof | [`test_send_receipts.py`](../services/chat-stream-v2/tests/test_send_receipts.py), [`test_send_receipt_late_proof.py`](../services/chat-stream-v2/tests/test_send_receipt_late_proof.py); daemon unit / focused | Real store projection and direct Server dispatch with synthetic records/events. Inspected: metadata correlation, event stamping, proof reconciliation/race/ambiguity and access checks. Distinct from physical-send and client protections. |
| Disconnect and client reconciliation | [`test_send_survives_disconnect.py`](../services/chat-stream-v2/tests/test_send_survives_disconnect.py); [`test_inbound_admission_bounds.py`](../services/chat-stream-v2/tests/test_inbound_admission_bounds.py) (reference examples below); [`optimisticReconciliation.test.ts`](../pentacle-chat-core/tests/optimisticReconciliation.test.ts) | Server handler/task-lifetime tests use fake sockets and send handlers; no Comms delivery. Client reducers/selectors use synthetic state/events: identity, live/snapshot replay, status ordering and caption lattice. Client suite runs in its own required frontend step. One complete real send/disconnect/consumer journey remains UNKNOWN across the repo, not a proven absent test. |
| Provider/account startup and recovery | Daemon `test_provider_*.py`, `test_account_*.py`, especially [`test_account_repin_state_machine.py`](../services/chat-stream-v2/tests/test_account_repin_state_machine.py); daemon unit / selector `provider_startup` | Inspected repin examples preserve generation, one native proof/login, no sealed-op replay and superseded bindings with injected refusals; table wrappers expand subcases. Exhaustive marker is excluded from merge. Provider/native account and launch-environment fixtures are not live authentication/quota qualification; see selection's explicit limits. |
| Consent and operator questions | CLI [`test_consent_cli.py`](../services/agent-orch/tests/test_consent_cli.py); daemon/CLI `test_consent*`, `test_question*`, `test_prompt*`; root/UI counterparts | Inspected CLI parsing includes revision/generation, local-admin ceremony and emergency-revoke claims; lifecycle/transport/token calls monkeypatched. Daemon admission and browser question registry are different boundaries; complete cross-layer assertion audit UNKNOWN. |
| Session lifecycle and work coordination | Daemon/CLI `test_session*`, `test_spawn*`, `test_close*`, `test_work*`, `test_report*`; root `test/work*` | Existing daemon/CLI/root runners, and browser `web-work-lanes`, `scheduled-section`, closed-slot/reconnect scenarios. File families locate lifecycle, dispatch and work obligations; assertion semantics and real/fake dependencies outside the inspected send fixtures UNKNOWN. |
| Client rendering, history and workspace | Root `test/chat*`, `test/renderer*`, `test/sidebar*`; chat-core tests; browser registry/history follow-ons | Root component/DOM tests, standalone core and real-browser served candidate fixture journeys protect different boundaries. Selector `client_wire_render` names its inspected seam and version limitations. Actual provider turn round trip, arbitrary installed versions and native/mobile rendering are not certified here. |
| Satellite transport and pin transitions | Daemon `test_satellite*`, `test_event_push*`, `test_account_satellite_roots.py`; selector `satellite_transition` | Selector documents controlled-peer retry/dedupe and exact-pin fixtures. Real deployment/update/restart/rollback on installed hosts is a separate gate; individual assertions/fake boundaries outside cited selection evidence UNKNOWN. |
| Release/deploy invocation | Daemon `test_deploy*`, `test_release_selection.py`; selector `deploy_invocation`; root hook tests | Selector records subprocess invocation, refusal/evidence and rollback fixture checks. Source fixtures do not activate installed services or prove host health. Root [`pre_push_hook.test.js`](../test/pre_push_hook.test.js) protects hook behavior separately. |
| Voice, audio and microphone | `mic-server/test/`, daemon `test_voice*`, root `test/*voice*`/`test/*mic*`, browser voice/mic registry | Separate contracts pytest, daemon, root and browser entries. Synthetic audio/state/service fixtures do not establish physical microphone or remote ASR behavior. Per-file assertion/fake-boundary audit UNKNOWN. |
| Selection, environment and cleanup | [`test_select_tests.py`](../scripts/test_select_tests.py), [`test_gate_env.py`](../scripts/test_gate_env.py), daemon [`test_harness_reaping.py`](../services/chat-stream-v2/tests/test_harness_reaping.py); explicit self-tests / daemon unit | Inspected selector tests use real disposable Git objects and synthetic files to check mapping, rename/shared/missing-evidence fallback and invalid input refusal. Reaping tests inspect real owned processes, identity-safe refusal and a 60s hermetic isolation observation. That intentional cost alone is not BLOAT. |
| Dashboard and optional adapters | Vendor [`public_boards.test.js`](../vendor/dashboard-library/tests/public_boards.test.js), browser dashboard registry; other convention candidates in census | Vendor package/CI and browser entries are distinct. Optional integrations and unclassified filenames remain UNKNOWN until assertions and dependencies are inspected. |

Two additional disconnect/task-lifetime references in
[`test_inbound_admission_bounds.py`](../services/chat-stream-v2/tests/test_inbound_admission_bounds.py)
were inspected outside the 22-scenario assessment sample, at `1e3c3241` and
unchanged on this candidate. `test_detached_sends_keep_credit_until_terminal`
holds two accepted sends on an Event, closes and joins the client handler,
asserts both detached tasks remain and credit rejects another send, then
releases them and checks the exact effects plus renewed admission.
`test_detached_send_outlives_cancelled_hello_barrier` holds hello, pipelines a
send, closes the client handler, then checks the detached task is not cancelled,
its exact send effect and released durable credit. Both exercise real Server
task/admission code with fake sockets/handlers, not Comms or durable receipt
delivery. They use the existing daemon unit/focused runner; this static index
adds neither runtime proof nor rows to the original assessment.

**Concrete selection GAP:** root npm-test does not select the 44 standalone
chat-core `.test.ts` files or its typecheck. Inspection scope: root package
scripts, shallow npm runner, gate-env target map and Public checks workflow.
Run the existing standalone package test when that surface changes; no new
runner is introduced. This is a selection limitation, not proof the suite is
never run elsewhere. Other filename similarities do not establish gaps.

## Reference: send retry to receipt

Keep the existing test module, node names and shared fixtures. A text send uses
real `Comms.send` → `Store.claim_or_coalesce_send` → fake paste → durable receipt.
An optimistic ID identifies one logical message within recency; request IDs
identify attempts. Retries may mirror another replay's receipt, so the contract
does not require a direct link to the first request. Read-only contract sources:
[`comms.py`](../services/chat-stream-v2/comms.py) (`send`,
`_coalesced_send_result`) and [`store.py`](../services/chat-stream-v2/store.py)
(`claim_or_coalesce_send`, `get_send_receipt`).

The following existing nodes in `test_send_idempotency_dedupe.py` retain all
prior assertions. Only the first two gain assertions/interleaving control.

| Existing node | Failure protection retained; reference increment |
| --- | --- |
| `test_rotated_request_id_retry_collapses_to_one_delivery` | Four rotated request IDs: one paste, initial confirmation and every receipt exists. Added: each receipt's own request/target/optimistic identity, landed state/delivery and confirmation; each retry returns coalesced/do-not-resubmit. |
| `test_concurrent_overlapping_retries_coalesce` | One paste for overlapping retries. A local fake-transport subclass now holds winning paste with bounded events; retry must finish coalesced/accepted/unconfirmed while winner is held. Release/join tasks in `finally`; after release, assert one paste and both correlated receipts. No guessed sleep or requirement that replay updates to landed. |
| `test_new_optimistic_id_same_text_is_delivered` | Fresh optimistic identity preserves two intentional identical sends. |
| `test_post_restart_same_text_after_window_is_delivered` | Expired recency permits a new landed send, without coalescing. |
| `test_prior_not_landed_does_not_block_retry` | A genuinely failed attempt remains retryable with a fresh paste. |
| `test_committed_pending_proof_prior_replays_not_resubmit` | An in-flight committed send is replayed, with no paste and a correlated replay reason. |
| `test_failed_preclaim_superseded_by_not_landed_allows_retry` | Latest failure supersedes a stale accepted claim; both store claims remain noncoalesced. |
| `test_attachment_failure_then_recovery_retry_delivers` | Failed blob fetch makes no paste; recovery retry delivers once and does not coalesce. |
| `test_mobile_launch_id_scheme_unaffected` | Distinct launch optimistic identities each deliver (component test, not mobile execution). |
| `test_coalesced_replay_of_provider_queued_send_keeps_queued` | First replay retains provider-queued state/reason, no paste. |
| `test_chained_coalesced_replays_of_provider_queued_send_stay_queued` | Both chained replays retain queued marker and receipt suffix, no paste. |

Focused validation uses the supported `v2-pytest` target with these five files:
`test_send_idempotency_dedupe.py`, `test_send_semantics.py`,
`test_send_receipts.py`, `test_send_receipt_late_proof.py`,
`test_send_survives_disconnect.py` (each prefixed `tests/`). The invariant floor
is added by the wrapper. Baseline/candidate each executed 111 nodes with the
same Python 3.13 toolchain; observed pytest times were 3.51s / 3.34s on one
host, not a performance claim or whole-suite acceptance.
The reference file is currently unmapped in the advisory selector: its actual
change selects conservative broad fallback, including the full daemon merge
gate. The existing selection policy is preserved.

Two disposable-copy controls use the rotated-request node: bypassing the
coalesced return must fail the paste-count assertion; corrupting replay
state/confirmation while retaining one delivery must fail its landed receipt
assertion. Restore original product bytes and require focused GREEN. A setup
failure or timeout is not that RED. See the harness for receipts, final merge
and npm gates; the unchanged client/browser suites are indexed, not certified
by this focused flow.

Later bounded work can inspect client reconciliation, provider/account recovery
and the unclassified/manual entries before selecting another reference. The
current map/reference owner hands those priorities back with the work item;
this page does not commission conversion, physical moves or coverage reduction.
