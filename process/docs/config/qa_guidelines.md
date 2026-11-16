---
id: config_qa_guidelines
title: QA guidelines
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-09'
source_path: docs/config/qa_guidelines.md
tags:
- pentacle
- process
summary: QA guidelines.
related: []
---

# QA guidelines

## Spec review

Read the problem, intended behavior, constraints and validation plan before implementation. Identify ambiguous acceptance, missing negative cases, private-environment assumptions, dependency gaps and resource conflicts. Verify environment claims against evidence. Return actionable defects that must be resolved before validation locks; avoid inventing extra gates unrelated to the goal.

## Final candidate review

Read the implementation and tests independently against the frozen spec. Pin the candidate and relevant dependencies. Exercise the success path with real or representative inputs and meaningful negative controls. A test that exits before reaching the asserted mutation does not prove the mutation safe. Confirm evidence comes from the artifact and environment claimed.

When a test fails, classify the oracle and harness before attributing a product defect. Do not change production code merely to satisfy an invalid test. Preserve raw evidence and distinguish product, harness and cleanup failures. Enumerate all independent checks after a failure; stop only unsafe or prerequisite-dependent work.

Review configuration defaults, optional integrations, install/startup, error handling and cleanup where the change affects them. Inspect installed or packaged consumers for release work. Source review alone does not establish deployment or clean-room acceptance.

## Verdict contract

Return ACCEPT or REJECT with scope, candidate identity, checks run, evidence locations and limitations. A blocking finding identifies the violated acceptance criterion, file and location, reproduction, expected behavior, observed behavior and practical consequence. Advisory wording preferences cannot make an otherwise accepted candidate fail.

Separate accepted sub-surfaces from rejected ones so repair does not erase valid evidence. Do not claim unrun checks passed. After a valid rejection, re-review the repaired surface and relevant regression checks. Two valid rejections of the same surface require owner diagnosis and a recorded change in approach before another pass; changing a commit label does not reset this count.

## Documentation review

Cold-read the documentation against the implemented behavior. Follow setup and local links from the public layout, with private files and machine configuration absent. Check prerequisites, commands, config precedence, supported optional features and expected failure behavior. Identify which instructions were exercised and which remain unverified. Documentation that points to an inaccessible private dependency is not complete onboarding.

## Evidence and reporting

Keep raw output in the work item's `_artifacts/` directory. Bind receipts to the candidate, harness, configuration, runtime/toolchain and tested cells. Keep secrets and personal data out of public receipts. In Pentacle, file the commissioned structured completion report; a chat message does not substitute for it. Follow the [orchestration guide](agent_orchestration.md) for report and authority handling.
