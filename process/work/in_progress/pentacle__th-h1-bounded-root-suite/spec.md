---
id: spec_pentacle_th_h1_2026_10
title: TH-H1 bounded root suite
type: spec
status: in_progress
canonical: false
created_at: '2026-10-05'
updated_at: '2026-10-05'
source_path: work/in_progress/pentacle__th-h1-bounded-root-suite/spec.md
machine: shared
owner: dot
tags: [pentacle, test-harness]
summary: Bounded attributable root-suite execution with owned cleanup and synthetic proofs.
related:
- work_pentacle_th_h1_2026_10
---

# TH-H1 bounded root-suite execution

Owner: dot. Status: in_progress. Packet: TH-v1 rev0.4 (2026-10-05).

## Tracking

Authoritative repository: https://github.com/HJK6/pentacle
Base: main586942610bdaf54c079b57d3324bc87042eb6024.
Assigned branch: dot/th-h1-bounded-root-suite. Development began with a verified1224-blob materialization and now uses an
authentic Git checkout at the same pinned base, with branch
dot/th-h1-bounded-root-suite and a local object-cache origin. The authoritative
publication remote remains the GitHub URL above. Base and branch were read back. No user
computer, daemon, settings, secrets or production access is involved.

## Goal and scope

Make the root suite bounded and attributable without weakening assertions.
Preserve the pinned144-file discovery set, TypeScript build failure, pass/fail
exit semantics and the byte-identical existing HOME-isolation test. Add per-file
and absolute run budgets, owned-process-group cleanup, JSON summary and stderr
table. List every unrun source. Test-only fixture repairs are permitted after
reproduction; product defects stay red and are reported.

## Validation plan

Independent preimplementation review checks discovery, HOME behavior, cleanup
ownership and unsafe live-call paths. Preserve bounded baseline RED output.
Use synthetic pass/fail/hang/build-fail/child-survivor fixtures and a manifest.
Negative controls cover whole-run scheduling cutoff, timeout-then-zero exit,
unrelated process safety and incomplete-cleanup reporting. Bundling counts
against the absolute budget. Final exact-head install, root suite and Node
syntax checks are reported with measured time and slowest ten files. All
unavailable full-capability checks are explicitly fleet-owned.

## Constraints and assumptions

No existing test deletion/skip/xfail/assertion weakening, dependency/CI/pin
changes, merge/rebase/force-push or product changes. Use POSIX process groups
created by this runner only; never name-match processes. Cleanup has a separate
short bounded grace after scheduling deadline; inability to inspect or kill
is an explicit non-success, never proof of cleanup. One isolated HOME remains
shared across the suite as before. Packet access-check branch is unchanged.

## Current checkpoint

Runner and test-only microphone fixture repair implemented. Pinned144-file
discovery preserved; three unsafe unchanged files are explicitly unrun locally.
Pristine safe baseline141files/1118tests passed17.971s; controlled synthetic
hang and missing-global-WebSocket RED evidence preserved. Initial candidate
safe142files/1133tests passed62.978s. Independent review identified late-exit,
cancel-before-spawn and directory-cleanup attribution gaps; bounded repairs and
negative controls are implemented and independently accepted. Final exact-head gate and fleet handoff next.
See docs/TEST_RUNNER.md; raw private evidence is retained outside this tree.
Raw local logs stay outside source distribution; public evidence is sanitized.
