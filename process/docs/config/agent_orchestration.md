---
id: config_agent_orchestration
title: Agent orchestration
type: config
status: stable
canonical: true
created_at: '2026-09-09'
updated_at: '2026-10-08'
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

Public role instructions are in [planner](../../agents/planner_baseline.md), [lead](../../agents/lead_baseline.md), [QA](../../agents/qa_baseline.md), [documentation](../../agents/documentation_baseline.md), [coordinator](../../agents/nexus_baseline.md) and [assistant](../../agents/assistant_baseline.md) baselines. Without orchestration software, load the relevant baseline into the agent and store its review verdict alongside the spec.

### Stage authority

Role label, model and effort, task and authority are four separate facts; none implies another.

- A **planner** writes the spec and commissions its independent spec review. It does not implement, and it does not acquire execution authority by relabeling itself.
- An **execution lead** implements only under an explicit commission: a brief or spawn that names provider, model and effort, role and spec, issued after the spec is accepted. An authored spec or a self-chosen label is not that commission.
- **QA** is independent of the author and the implementer, reviews once, reports a verdict and stops.
- Hand accepted scope to the named execution owner once, at the stage boundary. Routine edits inside an owner's existing scope need no further transfer or approval.

### Choosing a coordinator

Prefer an existing suitable coordinator for lanes that share a real need: a product surface, an artifact, a release, shared files or a deployment window. A single lane needs only a lead. Do not insert a coordinator into nearly finished work when the transfer costs more than it saves. Grouping grants no new authority and never holds unrelated authorized work while a group is formed. Adopt running lanes by transfer, keeping their accepted specs, reviews, grants and pending questions, without pausing or re-reviewing them. Combine compatible changes that are ready for the same release into one candidate, landing order and activation window, and never hold a ready critical fix for an unready optional one.

## Recommended operating profile

As of 2026-10-08. This profile assigns by task class and risk, without requiring an escalation ladder. It is a dated process recommendation, not a guarantee of provider/account availability. Check `agent-orch models --help` and the installed model catalog, then verify the requested account supports the tuple. Unsupported choices must fail explicitly rather than silently substitute. Operator overrides and available providers govern each deployment; private quota/reset policies do not travel with this kit. The public CLI catalog is in [spawn_profiles.py](../../../services/_shared/spawn_profiles.py).

| Work | Implementation | Independent QA |
| --- | --- | --- |
| Routine: bounded scope, clear method, objective acceptance | `codex / gpt-6-luna / max` | `codex / gpt-6-luna / max` |
| Implementation judgment: accepted design, cross-file interactions, bounded failure | `claude / claude-sonnet-5-5 / high` or `codex / gpt-6.1-sol / medium` | `codex / gpt-6.1-sol / medium` or `claude / claude-opus-5-5 / medium`, independent of the implementer |
| Demanding reasoning: competing causes, subtle invariants, hard regressions | `claude / claude-opus-5-5 / medium` or `codex / gpt-6.1-sol / high` | As for implementation judgment, or the hard-problem path when the risk warrants it |
| Hard problem: reframing, deep root cause, substantial investigation and fix | `codex / gpt-6-astra / high` as a scoped lead | A reviewer from the other provider, named explicitly in the commission |
| Planning and bounded advice | `codex / gpt-6-astra / high` or `claude / claude-fable-5-1 / high` | Spec QA chosen by the class of the work being specified |
| Multi-lane coordinator | `claude / claude-opus-5-5 / high` | None of its own; each lane carries its QA |

- Complexity and risk choose the row. File count or patch size alone is not a reason to escalate, and you select the required row directly rather than failing at lower rows first.
- The two columns are independent. The implementation tuple never selects or limits the QA tuple; record the QA tuple and its risk rationale separately.
- Effort is part of the tuple. Name it for the actual task; do not inherit it from the implementer or the author.
- If a chosen tuple is unavailable, record the constraint and the authorized substitute. Do not silently lower the tier.
- The coordinator tuple is for coordination; it is not the default implementer. A planning model is not a substitute hard-problem solver.

For new planned work the order is planner, independent spec QA, then one execution lead or an optional multi-lane coordinator. Drivers consume the accepted packet, implement directly and own closure. The planner closes after its spec is accepted unless it is doing named, bounded work; the continuing execution owner takes custody of its child reports and pending decisions first. Advice on a named blocker, a materially changed scope or a two-rejection reassessment is commissioned fresh, reports once and closes. Bounded repairs preserve accepted evidence for unchanged scope; two valid rejections require diagnosis reassessment.

A lead keeps at most two live children. Run no standing evaluator, shadow or polling seats. Read each durable child report once when it is ready, then close the completed child.

### Overrun evaluation

When a drive reaches the overrun trigger in the [development process](development_process.md#keeping-delivery-finite), the owner makes one evaluation request per plan epoch, recording the original start and estimate, the threshold crossed, the evidence and the one decision needed. The owner commissions one fresh, bounded evaluator from the planning row. The evaluator weighs what reached a runtime, rulings against ceremony, idle time and the smallest remaining user outcome, and recommends one of: ship accepted work and track residuals; narrow; pivot; or put one decision to the operator. It reports once and closes, it does not implement, and its advice does not replace independent QA. A further evaluation needs a materially changed finding, not the passage of time.

## Optional Pentacle integration

This section requires a separate Pentacle installation providing the daemon and agent-orch CLI. The process bundle contains workspace scripts and role instructions, not those runtimes. Without that installation, use the file-based ownership and review workflow above.

Point `AGENT_ORCH_MEMORY_REPO` at your spec workspace for supported agent-orch memory and role-baseline discovery. Configure the daemon's spec-memory setting separately as described in [workspace setup](../../README.md); one shell variable does not configure every consumer. Use your installed `agent-orch --help` and subcommand help for the supported command contract.

Give a session a durable title that names the lasting product or goal, and give visible sessions a status card with a goal and plan. The card states the current phase and the next action, or what the seat is waiting for and from whom; refresh it at every phase change and keep the durable checkpoint in the spec. A lead sets an estimate on its first card with `agent-orch status --eta`, refreshes it at each milestone or decision request, and flags an overrun above 50% in its next decision request; a refreshed forecast does not reset the original estimate. Bind an ownership seat to its spec. For delegated work, include the objective and explicit provider/model/effort, and keep workers hidden unless operator visibility is needed.

Commission an execution lead with `--role lead --spec-id <spec id>` in the spawn request itself and name both in the brief; prose alone does not set the seat's metadata. Read the created seat back once with `agent-orch inspect` and confirm its role and attached spec before the lead commissions QA.

Use `agent-orch tell` for peer context and `agent-orch report` for a commissioned completion. Report START, END, BLOCKER and GATE decisions concisely; keep full evidence in artifacts. A peer END message does not replace a completion report.

A `child_idle_unreported`, explicit delivery failure, or apparent missing terminal report is an anomaly: inspect the child once. A missing notification does not mean the typed report is absent. If a durable report exists, read and use it once with `agent-orch inspect` or `agent-orch await`.

If no terminal report exists, the current CLI has no `recover` verb. When an independent QA close gate remains, commission fresh QA for the same pinned candidate and evidence in a parented seat. Use `agent-orch spawn` with the review scope and explicit model selection:

```sh
agent-orch spawn --parent "$AGENT_ORCH_STREAM_ID" \
  --provider "$QA_PROVIDER" --model "$QA_MODEL" --effort "$QA_EFFORT" \
  --role qa --phase qa --spec-id "$SPEC_ID" \
  --objective "$REVIEW_OBJECTIVE" --no-self-close-on-completion
```

The parent reads the typed report and closes the seat. `await-spawn` handles spawn creation and prompt delivery, not completion.

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
