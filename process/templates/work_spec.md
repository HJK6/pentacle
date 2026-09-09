---
id: spec_example_2026_01
title: '{{title}}'
type: spec
status: backlog
canonical: false
created_at: '2026-01-01'
updated_at: '2026-01-01'
source_path: work/backlog/example-repo__example_topic_2026_01/spec.md
machine: '{{machine}}'
owner: '{{owner}}'
tags: []
summary: '{{summary}}'
related:
- work_example_2026_01
---

# {{title}}

## Status

Drafted {{date}}. Scope: <single-user | multi-user>, <big-bang | phased>.

## Ownership

- Authored on: {{machine}}
- Implementation: {{machine}} (every machine touched; frontmatter `machine` is the primary one, `shared` when multiple machines are involved)
- Driver: {{owner}}

## Tracking

- Lookup title: {{title}}
- Status: {{status}} (frontmatter `status:` must always equal the parent folder name)
- Completion rule: every Acceptance Criteria box checked or annotated `(waived: <reason>)`, then Closure per `docs/config/development_process.md`.

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

### Custom

- [ ] <spec-specific outcome 1>
- [ ] <spec-specific outcome 2>

## Risks

- <Risk> — mitigation: <mitigation>

## Non-Goals

- <Explicitly out of scope>

<!-- At closure add a `## Retro` section (3–10 lines: surprises, debt, follow-ups filed as backlog specs)
     and `completed_at: 'YYYY-MM-DD'` to both spec.md and summary.md. The validator enforces both. -->
