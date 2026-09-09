---
id: config_agent_orchestration
title: Agent orchestration
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-09'
source_path: docs/config/agent_orchestration.md
tags:
- pentacle
- process
summary: Agent orchestration.
related: []
---

# Agent orchestration

## Roles and ownership

Use a lead for spec ownership and implementation. Use workers for bounded investigation and independent QA. Add a coordinator only when multiple owners or shared-resource decisions need coordination. Choose models and effort explicitly according to task complexity and your own available budget; no provider account or private fleet is required by this process.

Each brief states the objective, spec, owner, scope, input artifacts, allowed mutations, resource constraints, acceptance contract, report destination and termination authority. Work on independent slices concurrently. Do not delegate implementation down a chain that leaves no one accountable for the failing journey.

Public role instructions are in [lead](../../agents/lead_baseline.md), [QA](../../agents/qa_baseline.md), [documentation](../../agents/documentation_baseline.md) and [coordinator](../../agents/nexus_baseline.md) baselines. Without orchestration software, load the relevant baseline into the agent and store its review verdict alongside the spec.

## Optional Pentacle integration

This section requires a separate Pentacle installation providing the daemon and agent-orch CLI. The process bundle contains workspace scripts and role instructions, not those runtimes. Without that installation, use the file-based ownership and review workflow above.

Point `AGENT_ORCH_MEMORY_REPO` at your spec workspace for supported agent-orch memory and role-baseline discovery. Configure the daemon's spec-memory setting separately as described in [workspace setup](../../README.md); one shell variable does not configure every consumer. Use your installed `agent-orch --help` and subcommand help for the supported command contract.

Give a session a durable title and visible sessions a status card with a goal and plan. Update the card at milestones; keep the durable checkpoint in the spec. Bind an ownership seat to its spec. For delegated work, include the objective and explicit provider/model/effort, and keep workers hidden unless operator visibility is needed.

Use `agent-orch tell` for peer context and `agent-orch report` for a commissioned completion. Report START, END, BLOCKER and GATE decisions concisely; keep full evidence in artifacts. A peer END message does not replace a completion report. Before recovering a missing report, inspect the child and treat recovered evidence as lower trust.

Ask for user input only when a missing decision or action uniquely belongs to the user. A visible seat uses `agent-orch prompt ask` for an asynchronous durable question and continues independent work. Under a coordinator, workers route questions to it. An operator action uses Done / Not yet; a decision offers choices that change the next action. Silence and elapsed time provide no authority.

Never close another seat or use a report's terminate option without the corresponding grant. A successor handoff includes the current spec and evidence, and a named owner. Avoid blocking waits when independent work remains; use durable completion notifications and bounded retrieval.
