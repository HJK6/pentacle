---
id: config_development_process
title: Development process
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-10-08'
source_path: docs/config/development_process.md
tags:
- pentacle
- process
summary: Development process.
related: []
---

# Development process

## Scope and ownership

A lead owns a spec from requirements through closure and implements it directly. Workers investigate bounded questions or provide independent QA. A coordinator is useful when multiple owners share dependencies or runtime resources; one lead can work without one. Parallelize independent ownership lanes and checks, while serializing conflicting mutations.

For a tiny reversible change, compress the paperwork and review round trips to fit the risk. For meaningful code, build, deployment or workflow changes, record the goal, current behavior, desired behavior, non-goals, constraints, owner, acceptance criteria and validation plan before editing. Choose automated checks by default. Reserve a manual gate for behavior that needs an actual human or external event.

Size the request before writing a spec. Keep one bounded, independently shippable outcome in each spec. When several specs serve one goal, group them under that goal and record their order there.

New planned development starts with a planner, independent spec QA, then an execution lead consuming the accepted spec packet and establishing standing authority. A single lead is enough for one lane; an optional Nexus coordinates multiple separately owned lanes. The lead implements directly. Workers may investigate, check environments or independently review. The planner closes once its spec is accepted: it publishes a resumable checkpoint and the transfer packet, files its report and ends, unless it is doing named, bounded work. Waiting for execution or being available for questions is not such work. Later advice on a named blocker, a materially changed scope or a two-rejection reassessment is a fresh bounded commission that reports once and closes. [Stage authority](agent_orchestration.md#stage-authority) separates the planner, execution lead and QA; choose each tuple from the [operating profile](agent_orchestration.md#recommended-operating-profile).

Before editing, record and compare intended versus actual checkout toplevel, full authoritative remote URL, branch and base ref/SHA in the spec's Tracking section. Read them back with `git rev-parse --show-toplevel`, `git remote get-url origin`, `git branch --show-current` and `git merge-base HEAD <base-ref>`. Freeze the final full candidate SHA (or content identity before commit), reviewed scope and gate evidence digest for independent final QA. Local identity, runtime configuration and real receipts stay in the private workspace.

## Spec lifecycle

Each item contains `spec.md` and a short `summary.md` under `work/<status>/<repo>__<topic>/`. Keep frontmatter status and source paths aligned with the directory. Keep lightweight evidence pointers in `_artifacts/`, with external raw artifact location and SHA256; private raw logs/assets stay outside the public kit. Keep the spec readable, with one current checkpoint rather than an append-only transcript. [Memory authoring](../../MEMORY.md) describes facts, preferences and decisions separately from work lifecycle.

| Status | Required next outcome |
| --- | --- |
| `backlog` | Gather the problem, owner and intended acceptance. |
| `analysis` | Resolve assumptions; independently review the spec and validation plan. |
| `ready_for_dev` | Scope and validation are locked and ready for implementation. |
| `in_progress` | Implement, run focused checks, finish required final validation and review. |
| `needs_qa` | Only a genuine human-only acceptance gate remains. |
| `blocked` | Record the exact unmet prerequisite, its owner and resumable next action. |
| `completed` | Satisfy closure, acceptance criteria and the retrospective. |
| `deprecated` | Record why the item was superseded or abandoned and its successor if any. |

Use the [workspace tools](../../README.md) to create items and validate their metadata. A catalog is generated from the working tree; it does not override the spec. Synchronization and Git ownership are deployment choices. Use one catalog writer in a replicated workspace, and do not assume another team's sync service is installed.

## Diagnosis and validation

Start with the failing user journey on the actual runtime or an exact isolated copy. Capture RED before a fix. Identify the first failed predicate and classify it as product failure, harness error or cleanup failure. If classification is unresolved, record competing hypotheses and the observation that will distinguish them.

Trace wrappers, delegation, configuration loading and installed consumers. Inspect the source and artifact that actually execute; a correct helper in a development checkout does not repair a scheduler using an older installed copy. Compare content when commit labels differ.

Define observable success and meaningful negative controls. Tests must prove that a broken implementation would fail. Do not weaken assertions, absorb failures into retries or mark an unresolved failure expected to obtain a green result. Make exclusions explicit and verify their scope.

After any failure, stop unsafe dependent mutations. Continue every independent non-mutating check and enumerate all failures. Mark a skipped check with the failed prerequisite rather than implying it passed. Rehearse the whole safe pipeline before an expensive runtime retry. Derive timeout and retry bounds from observed behavior and the contract.

Run focused gates during repair and the final required gate on the candidate. Run platform-specific checks on the platform that ships. Remote CI complements local pre-push gates; a remote green result cannot erase a local failure. Record unavailable checks as limitations with an owner and prerequisite.

After a failed live test, keep the evidence it produced, such as a recording, capture or log, together with its configuration. Reproduce and classify the failure offline against that retained evidence and prove the fix on the same evidence, then run one live confirmation. If the confirmation fails, return to diagnosis; do not retry live automatically. State what an offline replay cannot establish.

## Readiness and release validation

### Ready before human time

Before asking a person to perform a physical step, such as connecting a device, build the final artifact in the configuration the spec requires and verify that it reaches its intended first screen. The readiness request carries the artifact identity, its configuration and, for a user interface, a screenshot from the observed runtime. A running process alone is not readiness. If the source branch moves while the step is pending, rebuild and recheck before claiming readiness. If the screenshot itself needs the person's device, finish everything automatable first, ask only for that prerequisite and label simulator proof as simulator proof. Never start a build while the person waits.

### Scoped release validation

Choose release checks from the changed behavior and the release mechanism before committing to an estimate. Record the changed surface, the checks it requires, the baseline evidence being reused and the risk each release-blocking check covers. File count or a nearby broken harness does not by itself require full certification. Full certification is required when the change touches runtime or native dependencies (a lockfile change to a non-development runtime package counts), shared harness or provenance machinery, or cross-cutting behavior, or when the baseline cannot support the reuse. A scoped release is never labelled fully certified.

Before a certified run, complete one non-promoting rehearsal on real adapters: the same runtime, isolation, subject, scenario plan, cleanup and final validators. Mocks and hash checks do not establish this. Within one granted rehearsal scope, repair demonstrated harness defects against focused reproductions without asking again for each defect; stop on changed product behavior, ownership or acceptance. Never relax a guard or promote rehearsal output. When a command fails, save its bounded, redacted output, exit status, timing and target identity in the failure receipt before throwing or cleaning up.

## Independent QA and bounded repairs

Use [QA guidelines](qa_guidelines.md) for spec, implementation and documentation review. Freeze the review scope and candidate at dispatch. One valid rejection permits a bounded repair and a review of the repaired surface plus relevant regression checks. Preserve accepted evidence for unchanged inputs. After two valid rejections of the same surface, reassess the diagnosis and record a pivot, corrected scope or different investigative approach before continuing.

One fresh independent final review includes code, tests and a cold read of the as-shipped docs; do not commission a duplicate documentation review of the same candidate. [Typed reports](agent_orchestration.md#typed-completion-reports) bind QA and readiness to actual evidence. A lead's ordinary completion report is not a QA verdict.

Do not make tracker wording a runtime gate or commission a full fresh review for an advisory edit. Evidence reuse requires unchanged relevant artifact, harness, interpreter/toolchain, environment/configuration and tested scope. Source/build proof alone does not prove runtime activation.

## Deployment and cleanup

Use the granted deployment window and coordinate shared resources. Before mutation, record the installed preimage, candidate, rollback action and acceptance checks. After deployment, bind readbacks to the actual installed artifact and live process on each affected host. Name source-mode exceptions. A checkout HEAD or marker file alone does not establish the running version.

Harness cleanup covers every post-acquisition path, including admission refusal and exceptions before the main run function. Release only owned sessions, images, locks and other resources. Report expected versus actual cleanup counts and remaining failures. Retry only after the relevant admission conditions hold; do not silently widen resource thresholds.

### Shared hosts

Before requesting or extending a quiet window on a shared host, record what must run there, which heavy jobs can move and where, and what must stay and why. Measure capacity with one bounded check; do not infer it from memory size or from one other job being present, and treat model-account headroom as a separate input. Agree moves with the affected owners at a safe boundary. Never kill, migrate or duplicate another owner's job to manufacture quiet. A repeat attempt names a changed condition or an open diagnostic question. Do not apply an informal threshold stricter than the reviewed gate, and never silently waive one.

## Keeping delivery finite

Close a lane when its agreed outcome and required acceptance are met. Discretionary hardening, cleanup and test expansion found along the way go to the backlog with the observed need and a resume trigger; recording them neither authorizes them nor makes them release blockers. A follow-up blocks delivery only when it defeats the scoped acceptance or exposes a concrete defect in the changed journey.

At the first unrelated harness dependency that threatens delivery, or at the earlier of the original estimate plus 50% and four hours, the owner chooses and records one of three dispositions: ship through an already authorized adequate path; separate the optional work; or continue one named indispensable repair to one finite checkpoint, stating the outcome still missing and what happens at that checkpoint. Do not silently reset the original estimate, renew the same hardening scope or start a successor to reset the clock. The owner may commission one fresh evaluator per plan epoch for a bounded recommendation; see [overrun evaluation](agent_orchestration.md#overrun-evaluation).

## Closure

A work item closes with one explicit disposition:

- **Shipped:** the accepted change is merged and pushed to the target development branch, and activated if it changes a runtime.
- **Tracked:** remaining changes are committed and pushed to a named branch with an owned, resumable work record describing what remains. This does not satisfy an original request to ship when shipping prerequisites are still owned and actionable.
- **Handed off:** a named successor accepts ownership with the spec, artifact identity, authority, evidence and next action intact.

Check or explicitly waive every acceptance criterion with its reason and authority. Keep owned working trees clean. Include a short retrospective of lessons, not raw logs. Keep unfinished requirements in the active item; use separate backlog specs only for actual out-of-scope work. Do not use a deadline or a growing backlog to redefine success.

## Keeping derived guidance in step

If you keep private guidance derived from this kit, treat the shared generic text as part of the same work. When a private improvement changes reusable behavior, guidelines or workflow, land the generic version here in that work, rewritten for an independent reader with identities, configuration, data and infrastructure details removed, or record why the change is private-only. Keep a simple map from each private source file to its generic counterpart and the source revision it was last brought in step with, so that a later change which skipped the generic copy is visible. Do not close the improvement while its generic update is unowned or unfinished.
