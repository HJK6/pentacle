---
id: spec_kind_fixture__estimate_loop_demo_2026_10
title: 'Kind fixture: estimate loop demo'
type: spec
status: backlog
canonical: false
created_at: '2026-10-07'
updated_at: '2026-10-07'
source_path: work/backlog/kind-fixture__estimate_loop_demo_2026_10/spec.md
machine: triforce
owner: agents
tags:
- kind-fixture
- feature
summary: Synthetic work item produced by new_work_item.py for the usage rollup kind
  fixture.
related:
- work_kind_fixture__estimate_loop_demo_2026_10
---

# Kind fixture: estimate loop demo

## Status

Drafted 2026-10-07. Scope: <single-user | multi-user>, <big-bang | phased>.

## Ownership

- Authored on: triforce
- Implementation: triforce (every machine touched; frontmatter `machine` is the primary one, `triforce` when fleet-wide)
- Driver: agents

## Tracking

- Lookup title: Kind fixture: estimate loop demo
- Status: backlog (frontmatter `status:` must always equal the parent folder name)
- Completion rule: every Acceptance Criteria box checked or annotated `(waived: <reason>)`, then Closure per `docs/config/development_process.md`.
- Every new lead records the development checkout toplevel, full URL of the authoritative development remote and its base ref/SHA (for example `origin`/`origin/main`) in § Tracking, reads them back with `git rev-parse --show-toplevel`, `git remote get-url <remote>`, `git branch --show-current`, and `git merge-base HEAD <base-ref>` before the first edit, and links § Tracking in START; compare intent to actual state.

## Goal

<One paragraph: why this work exists and what changes for the user when it ships.>

## Current State

<What exists today: paths, files, behavior, evidence.>

## Target State

<What exists after the work: observable, binary.>

## Plan

1. Requirements gathering: confirm goal, constraints, affected repos/machines, validation target, done criteria with the user; write them back into this spec.
2. <Step>
3. <Step; for cross-machine work, per-machine rollout in blast-radius order>

## Validation

<How we know it worked: automated tests by default (name the commands), manual QA checklist only for a genuine human-only gate, and the evidence to capture.>

## Estimate

Fields, ratios and the closure rule: `work/README.md` § Estimate. The lines below are immutable once filled; actuals are appended at closure.

- estimated_at: 2026-10-07T12:09:32Z
- basis: none
- kind: feature
- elapsed_delivery_h: <p25>–<p75> (median <m>)
- usage: <provider/account>: <p25>–<p75> % of week (median <m>); <$ API-equiv range>
- assumptions: <named>

## Acceptance Criteria

### Common

#### Backlog
- [ ] **Requirements gathered** — goal, constraints, affected repos/machines, validation target, and done criteria recorded above before implementation planning.
- [ ] **Validation plan drafted** — method, expected tests or checklist, and evidence required.

#### Analysis
- [ ] **Spec QA** — a QA agent reviewed this spec for ambiguity, missing criteria, risky assumptions, and missing validation; driver looped until implementable.
- [ ] **Validation plan locked** — finalized in this spec before dev starts.

#### In Progress
- [ ] **Development** — implementation matches the spec; scope changes are spec edits, not silent drift.
- [ ] **Unit tests** — each custom criterion has at least one test asserting observable behavior.
- [ ] **Code QA** — an independent QA agent reviewed implementation and tests.
- [ ] **Testing passed** — tests ran on real inputs; output captured.
- [ ] **Documentation written** — durable design info absorbed into the target repo's `docs/` against the as-shipped diff.
- [ ] **Doc QA** — a fresh doc QA agent cold-read the docs against the as-shipped diff.

#### Needs QA (only if the validation plan flagged manual QA)
- [ ] **Manual QA passed** — every checklist item complete or annotated `(waived: <reason>)`.

#### Closure
- [ ] **Code disposition** — every change is either *merged* (to the target repo's `main`, pushed to `origin`, and deployed when it ships to a runtime) or *tracked* (committed and pushed to a named branch on `origin`, with a follow-up spec in a non-terminal status naming the repo and branch ref; undeployed runtime changes are listed there).
- [ ] **Working tree clean** — no uncommitted edits attributable to this spec in any touched repo.
- [ ] **Retro** — `## Retro` section added (or `## Retro — none: <reason>`) and `completed_at` set in both files.
- [ ] **Actuals recorded** — `## Estimate` has the `actual` line with the ratio.

### Custom

- [ ] <spec-specific outcome 1>
- [ ] <spec-specific outcome 2>

## Risks

- <Risk> — mitigation: <mitigation>

## Non-Goals

- <Explicitly out of scope>

<!-- At closure add a `## Retro` section (3–10 lines: surprises, debt, follow-ups filed as backlog specs)
     and `completed_at: 'YYYY-MM-DD'` to both spec.md and summary.md. The validator enforces both. -->
