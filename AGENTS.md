# Pentacle agent instructions

Use this file as the common entry point for agents working in this repository. Keep machine identity, private paths, credentials and deployment grants in local configuration outside the public tree. Local instructions may add project details; they do not override the user's scope or authorization.

## Start with the task

Read the relevant spec and [development process](process/docs/config/development_process.md) before meaningful changes. Classify a small reversible edit as compressed work; otherwise establish scope, ownership, acceptance criteria and the validation plan before implementation. A coordinator is optional. The lead owns the result and implements directly; delegate bounded investigation or independent QA when it helps.

Specs live under `process/work/<status>/<repo>__<topic>/`. The working spec is authoritative; generated catalogs are lookup aids. Claim ownership before editing. Keep one current checkpoint with the next action and exact evidence locations so another agent can resume. See [setup](process/README.md) and [orchestration](process/docs/config/agent_orchestration.md).

## Diagnose and implement

Reproduce the actual failing journey before fixing it. Trace delegation to the implementation and inspect the artifact that runs. Classify product, harness and cleanup failures separately. A faulty test oracle does not establish a product failure or prove that the product passes.

On failure, stop unsafe dependent mutations, then continue all independent checks. Report the complete failure set with explicit prerequisites for checks that could not run. Do not discover one avoidable failure per expensive retry. Use focused checks during repair and the full required gate on the final candidate.

Keep changes scoped and simple. Do not add duplicate abstractions or preserve obsolete compatibility paths without a concrete consumer. Remove code and configuration that the change retires, including installed and scheduled consumers.

## Validate and review

Follow the [QA guidelines](process/docs/config/qa_guidelines.md). Meaningful work receives independent spec review before the validation plan locks and independent implementation review on the final candidate. Preserve accepted evidence within its actual input bounds. A rejection identifies an actionable contract defect with reproduction and file location; wording preferences are advisory.

After two valid rejections of the same surface, reassess the diagnosis and scope before commissioning another review. Do not restart a whole review for every small repair or use a new commit label to reset the cycle.

## Operate within authority

Proceed with authorized reversible implementation and checks. Ask only for missing decisions or actions the user alone must supply. Under a coordinator, route decisions to it. With Pentacle, use the durable asynchronous question and report protocol described in the orchestration guide; do not block independent work waiting for an acknowledgement.

Coordinate conflicting shared resources. Harnesses release only resources they own, including when admission fails after acquisition. Bind deployed acceptance to the installed artifact and running process, with preimages, rollback and readbacks for authorized runtime changes. Never interpret elapsed time or a deadline as approval.

## Close with evidence

Document the shipped behavior and setup. Keep raw receipts in the work item's `_artifacts/` directory, outside the public source distribution when they contain local data. Record artifact, harness, configuration and environment identity with acceptance evidence.

Close only as shipped, tracked or handed off under the development guide. Do not move unfinished acceptance requirements into a new backlog item to claim completion. A real out-of-scope defect may have a separate owned spec. Commit only owned files, run local gates before push, and leave a concise retrospective and a resumable record.
