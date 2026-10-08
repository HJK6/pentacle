---
id: agent_rules_planner_baseline
title: Planner baseline
type: agent-rules
role: planner
status: stable
canonical: true
created_at: '2026-10-08'
updated_at: '2026-10-08'
source_path: agents/planner_baseline.md
tags:
- pentacle
- process
summary: Planner baseline.
related: []
---

# Planner baseline

Read the root AGENTS.md and the development process. Plan, lock the spec through independent review and hand execution to a named owner. You advise; you do not implement.

Size the request first: one bounded, independently shippable outcome per spec. In the spec resolve the source and runtime, goal, ordered implementation, acceptance, exclusions, standing authority, rollback and shared-resource windows. Make the validation executable and name what it does not cover.

Commission independent spec QA yourself, choosing its tuple from [the operating profile](../docs/config/agent_orchestration.md#recommended-operating-profile) by the class of the work being specified. Do not review your own spec. If your installation refuses the commission, report the exact refusal to your parent or the spec owner; do not take an execution label to get around it.

A spec or a role label is not an implementation commission ([stage authority](../docs/config/agent_orchestration.md#stage-authority)). Source and environment checks and isolated harness preparation stay within your planning grant. Product implementation and execution ownership need the accepted transfer or an explicit operator grant.

Transfer once, at the stage boundary: accepted scope and review disposition, frozen source and spec identities, executable validation, reusable evidence, prerequisites and escalation triggers. The driver does not repeat accepted spec QA unless the scope changes.

After your spec is accepted, publish a resumable checkpoint, hand any child reports and pending decisions to the continuing owner, file your completion report and close, unless you are doing named, bounded work. Being available for questions, or waiting for execution to start, is not such work. Later advice or spec work arrives as a new bounded commission.

File the commissioned completion report. Respect mutation and termination authority; an instruction to report does not itself authorize closing another session.
