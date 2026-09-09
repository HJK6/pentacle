---
id: config_development_process
title: Development process
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-09'
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

## Spec lifecycle

Each item contains `spec.md` and a short `summary.md` under `work/<status>/<repo>__<topic>/`. Keep frontmatter status and source paths aligned with the directory. Store raw evidence in `_artifacts/`; keep the spec readable, with one current checkpoint rather than an append-only transcript.

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

## Independent QA and bounded repairs

Use [QA guidelines](qa_guidelines.md) for spec, implementation and documentation review. Freeze the review scope and candidate at dispatch. One valid rejection permits a bounded repair and a review of the repaired surface plus relevant regression checks. Preserve accepted evidence for unchanged inputs. After two valid rejections of the same surface, reassess the diagnosis and record a pivot, corrected scope or different investigative approach before continuing.

Do not make tracker wording a runtime gate or commission a full fresh review for an advisory edit. Evidence reuse requires unchanged relevant artifact, harness, interpreter/toolchain, environment/configuration and tested scope. Source/build proof alone does not prove runtime activation.

## Deployment and cleanup

Use the granted deployment window and coordinate shared resources. Before mutation, record the installed preimage, candidate, rollback action and acceptance checks. After deployment, bind readbacks to the actual installed artifact and live process on each affected host. Name source-mode exceptions. A checkout HEAD or marker file alone does not establish the running version.

Harness cleanup covers every post-acquisition path, including admission refusal and exceptions before the main run function. Release only owned sessions, images, locks and other resources. Report expected versus actual cleanup counts and remaining failures. Retry only after the relevant admission conditions hold; do not silently widen resource thresholds.

## Closure

A work item closes with one explicit disposition:

- **Shipped:** the accepted change is merged and pushed to the target development branch, and activated if it changes a runtime.
- **Tracked:** remaining changes are committed and pushed to a named branch with an owned, resumable work record describing what remains. This does not satisfy an original request to ship when shipping prerequisites are still owned and actionable.
- **Handed off:** a named successor accepts ownership with the spec, artifact identity, authority, evidence and next action intact.

Check or explicitly waive every acceptance criterion with its reason and authority. Keep owned working trees clean. Include a short retrospective of lessons, not raw logs. Keep unfinished requirements in the active item; use separate backlog specs only for actual out-of-scope work. Do not use a deadline or a growing backlog to redefine success.
