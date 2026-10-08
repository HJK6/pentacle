---
id: spec_pentacle_th_h5_2026_10
title: TH-H5 honest ops verdicts
type: spec
status: in_progress
canonical: false
created_at: '2026-10-05'
updated_at: '2026-10-08'
source_path: work/in_progress/pentacle__th-h5-honest-ops-verdicts/spec.md
machine: shared
owner: dot
tags: [pentacle, test-harness]
summary: Promotion and deploy verdicts that retain every cell and reason, exercised through synthetic seams.
related:
- work_pentacle_th_h5_2026_10
---

# TH-H5 honest ops verdicts
Base c56afde5eb4c49f6f021f9e67f5691221999a221; branch dot/th-h5-honest-ops-verdicts.
TH-v1 rev0.4 H5 expressly permits promotion/deploy verdict source changes. Only synthetic fake-gh and scripted-runner tests execute. No actual promotion, tag push, merge, deployment, SSH, fleet command or workflow/settings change.
Promotion: push tag once, bounded exact-identity required checks wait, maintain audited workflow/required coverage and CAS protections.
Deploy: retain every cell/reason, remote failure remains failure, incomplete local cannot be partial; complete local plus unreachable satellites is distinct non-success partial, never an automatic retry.
