---
id: config_agent_orchestration
title: Agent orchestration
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-09-28'
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

## Recommended operating profile (2026-09-28)

This Codex-first profile assigns by task need, without requiring an escalation ladder. It is a dated process recommendation, not a guarantee of provider/account availability. Check `agent-orch models --help` and the installed model catalog, then verify the requested account supports the tuple. Unsupported choices must fail explicitly rather than silently substitute. Operator overrides and available providers govern each deployment; private quota/reset policies do not travel with this kit. The public CLI catalog is in [spawn_profiles.py](../../../services/_shared/spawn_profiles.py); [official OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-6-sol) documents Sol's supported reasoning efforts separately from this operating policy.

| Work | Recommended provider / model / effort |
| --- | --- |
| Routine implementation, independent spec QA and final QA | `codex / gpt-6-luna / max` |
| Implementation judgment | `codex / gpt-6-sol / medium` |
| Demanding bounded reasoning | `codex / gpt-6-sol / high` |
| Exceptional hard-problem assignment | `codex / gpt-6-astra / high` |
| Multi-lane Nexus | `claude / claude-opus-5-5 / high` |
| Planning and retained advice | `codex / gpt-6-astra / high` or `claude / claude-fable-5-1 / high` |

An available `claude / claude-opus-4-8 / high` is an alternate implementation judgment lane; `claude / claude-opus-5-5 / medium` is an alternate demanding-reasoning lane. Fable is a planning/evaluator choice, not mandatory for every critical seat. For new planned work, planner → independent spec QA → one execution lead or optional multi-lane Nexus. Drivers consume the accepted packet, implement directly and own closure. The original planner advises only on named events through the owner. Bounded repairs preserve accepted evidence for unchanged scope; two valid rejections require diagnosis reassessment.

## Optional Pentacle integration

This section requires a separate Pentacle installation providing the daemon and agent-orch CLI. The process bundle contains workspace scripts and role instructions, not those runtimes. Without that installation, use the file-based ownership and review workflow above.

Point `AGENT_ORCH_MEMORY_REPO` at your spec workspace for supported agent-orch memory and role-baseline discovery. Configure the daemon's spec-memory setting separately as described in [workspace setup](../../README.md); one shell variable does not configure every consumer. Use your installed `agent-orch --help` and subcommand help for the supported command contract.

Give a session a durable title and visible sessions a status card with a goal and plan. Update the card at milestones; keep the durable checkpoint in the spec. Bind an ownership seat to its spec. For delegated work, include the objective and explicit provider/model/effort, and keep workers hidden unless operator visibility is needed.

Use `agent-orch tell` for peer context and `agent-orch report` for a commissioned completion. Report START, END, BLOCKER and GATE decisions concisely; keep full evidence in artifacts. A peer END message does not replace a completion report. Before recovering a missing report, inspect the child and treat recovered evidence as lower trust.

Ask for user input only when a missing decision or action uniquely belongs to the user. A visible seat uses `agent-orch prompt ask` for an asynchronous durable question and continues independent work. Under a coordinator, workers route questions to it. An operator action uses Done / Not yet; a decision offers choices that change the next action. Silence and elapsed time provide no authority.

Never close another seat or use a report's terminate option without the corresponding grant. A successor handoff includes the current spec and evidence, and a named owner. Avoid blocking waits when independent work remains; use durable completion notifications and bounded retrieval.

## Typed completion reports

The [agent-orch report contract](../../../services/agent-orch/README.md#completion-report-contract) is runtime authority; use the candidate CLI's `report --help`. These shell examples are templates for real commissioned seats and evidence, never commands to post fabricated reports to a live daemon. Set `CANDIDATE_SHA` to the frozen full 40-hex Git SHA and `GATE_DIGEST` to the SHA256 of the actual evidence index. A scope names paths and checked behavior, including exclusions.

Non-QA completion carries `summary`, `findings` and `next_action`; it does not claim QA acceptance:

```sh
agent-orch report --status done \
  --result '{"summary":"Owned implementation and focused checks complete","findings":[],"next_action":"Parent integrates and commissions final independent QA"}'
```

Independent QA binds the candidate, reviewed scope and evidence digest, then reports before its END tell:

```sh
agent-orch report --status done --qa-verdict accept \
  --target-sha "$CANDIDATE_SHA" --qa-reviewed-scope "$REVIEWED_SCOPE" \
  --qa-gate-evidence-digest "$GATE_DIGEST" \
  --result '{"summary":"Frozen acceptance reviewed","findings":[],"next_action":"Owner consumes scoped QA evidence"}'
```

Use `--qa-verdict reject` for a violated acceptance criterion with reproducible findings. A non-QA lead may claim readiness only after consuming a real independent accepting report for the candidate:

```sh
agent-orch report --status done --completion-kind implementation_ready \
  --target-sha "$CANDIDATE_SHA" \
  --qa-attestation-stream-id "$INDEPENDENT_QA_STREAM" \
  --qa-attestation-report-id "$INDEPENDENT_QA_REPORT" \
  --result '{"summary":"Independent final QA consumed","findings":[],"next_action":"Parent validates readiness and integrates"}'
```

The daemon checks the distinct QA stream, shared spec binding and prior durable accepting QA row. Readiness is consumable only when the daemon-owned attestation validation state is `verified`; reporter-written extras do not establish it. `PENTACLE_QA_ATTESTATION_MODE=warn` is the default: incomplete readiness may be persisted with an unverified warning. `enforce` rejects unverified readiness before insertion; `off` disables that rollout check. Ordinary terminal payload and QA-role field validation still apply. A written ACCEPT or END tell does not replace the typed report. File-based independent review remains valid when no daemon is installed; store the candidate, scope, verdict and evidence alongside the private spec.
