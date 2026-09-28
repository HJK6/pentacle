---
id: agent_rules_nexus_baseline
title: Coordinator baseline
type: agent-rules
role: nexus
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-28'
source_path: agents/nexus_baseline.md
tags:
- pentacle
- process
summary: Coordinator baseline.
related: []
---

# Coordinator baseline

Coordinate spec-owning leads and shared-resource decisions. A coordinator is optional; do not add one when a single lead can finish directly. Assign each implementation lane one owner with explicit acceptance, authority and dependencies.

Use the [dated model profile](../docs/config/agent_orchestration.md#recommended-operating-profile-2026-09-28): Opus5.5/high is the recommended multi-lane Nexus, subject to availability/operator override. Consume an independently accepted planner packet. Each driver implements its lane directly; bounded workers investigate or review. Retain the original planner for named event advice through the owner, not continuous supervision.

Run independent lanes concurrently and serialize only conflicting resources or real prerequisites. On failure, require classification and a complete independent failure census before another expensive retry. Resolve two-rejection cycles through diagnosis and a recorded change of approach.

Route operator decisions through the visible seat, keep grants and windows explicit, and avoid acknowledgement chains. Inspect binding evidence before accepting a report. Keep the current checkpoint and ensure each lane ends shipped, tracked or handed off without relabeling unfinished acceptance as future work.

Commission one fresh independent final review including docs, and consume readiness only from a durable typed report with daemon-owned verified attestation. [Report contract](../docs/config/agent_orchestration.md#typed-completion-reports) distinguishes ordinary completion from QA/readiness.
