# Repository workflow

Public `main` is the source of truth for development and runtime releases.
Start new branches from freshly fetched public `main` and land them here after
the applicable local gates, independent review and exact-SHA CI checks.

## Private-data exceptions

> **Note (2026-10-04):** the former `HJK6/pentacle-private` source repository is
> retired and archived read-only; public `main` is the sole source of truth.
> The private→public reconciliation/publish flow and the private-line merge-back
> described in this section are **historical** — there is no active private repo
> to project from. The guidance below is retained for context and for the general
> rule that private data (credentials, host profiles, signing inputs) stays out of
> the public tree.

Use a private branch only for a named need involving private data. Prefer local,
gitignored configuration when that is enough: deployment settings, credentials,
host profiles and signing inputs stay outside the public tree. Private code is
a derivative of accepted public code, with its private overlay kept explicit.

When an exception has a public-safe product change, reconcile it immediately
through a clean candidate based on current public `main`. Review the shared
content delta and exclusions, preserve public-only accepted changes, strip the
documented package overlay, and publish safe content without private ancestry.
Do not blindly replay a historical private diff or defer reconciliation into a
sync backlog. A reviewed private-only path manifest remains the exception
boundary; it does not make every excluded file confidential by definition.

## Landing on `main`

`main` is branch-protected: push the exact commit to a branch, let the
`Public checks` workflow go green on that SHA, then fast-forward `main` with an
explicit refspec (`git push origin <sha>:refs/heads/main`) and read it back
with `git ls-remote`. Private exception projections retain their source-to-public mapping receipts.
External contributions and normal development use this same public main line.

For web/daemon main landings, the public promotion helper adds an exact-run guard before
that fast-forward. From this public checkout, set
`PENTACLE_GITHUB_REPOSITORY=HJK6/pentacle` and run
`python3 tools/merge_gate.py promote --candidate <full-sha> --run-id <push-run-id>`.
It accepts only a completed successful branch-push `Public checks` run for that
SHA on the same repository and current branch tip. The candidate's tracked
`.github/workflows/predeploy-tests.yml` must match the audited command digest,
and the run's job steps must prove Node, residue, web, v2/CLI and source-integrity
checks succeeded. It creates and verifies the annotated `v2-gate/<sha>` tag,
then advances `main` with a compare-and-swap push. Read back remote `main` and
the tag after promotion. The private repository has its own mapped smoke
workflow; an unknown or mismatched repository is refused.

The tag push starts another required workflow run. The helper now waits in
one invocation under a single absolute checks budget (default 900 seconds, above the eight to ten minutes a real tag run takes;
--checks-timeout-seconds). It pushes a missing annotated tag once, then considers
only the newest matching push run/attempt for the exact candidate SHA, tag name,
mapped workflow and repository. An older or unrelated green run cannot satisfy
the gate. Each polling request is bounded by the remaining budget, including
late-response rejection. Public required job/step coverage remains mandatory.

It reports still_running, required_checks_missing, final_red and timeout as
non-success; only stable green evidence allows the existing main CAS. The exact
remote annotated tag object and peel are revalidated after waiting. Re-entry
still skips a redundant tag push only when remote and local tag objects match.
Keep the pre-push guard enabled and preserve refusal receipts. This source
change does not authorize dot to execute a promotion or mutate main.

## Push guard

Before every public push, run `python3 scripts/check_public_residue.py`, review
the exact candidate diff for private data, endpoints, local paths and credentials,
and compare fleet-name hits with public `main` using the existing no-new-hit
policy. A new hit requires sanitization or a scoped coordinator ruling. The
residue checker detects portable privacy rules and fixed deployment contracts;
also supply an external private dictionary to check actual private names. Its
digest/count receipt distinguishes a completed private check from `not_run`.
See [privacy rules and frozen fixture exceptions](public_release.md#public-identity-and-fixture-policy).
The guard does not replace private-content review. Its optional fixed mobile
synthetic profile supports that client's existing symbolic identifiers. Mobile
vendors its reviewed checker with a source hash receipt.


Install the pre-push guard once per clone with an absolute `core.hooksPath`
([developer onboarding § 8](developer_onboarding.md#8-pushing-safely)). It
refuses pushes that carry foreign history, omit the refspec, or aim a remote
named `public` at the wrong repository. A refusal is a finding to resolve before retrying.

## Hosted instances follow `main`

A persistent web host ([hosted profile instances](ARCHITECTURE.md#hosted-profile-instances))
runs accepted public code. For runtime-affecting releases, use the assigned
activation window: fast-forward its checkout, run
`npm run build:web`, restart the service, then read back `/login` (200),
`/api/config` without a cookie (401) and the daemon connection. Record the SHA
and PID of what is actually running; a checkout at the right commit is not a
deployment. Documentation and guard-only changes do not require a runtime restart.

Historical private exclusions are reviewed by content; ordinary source and portable tests move through the same public gates. See [Public source boundary](public_boundary.md) for the shipped batch and retained private inputs.

### TH-H5 promotion evidence details

The new bounded wait applies to the audited public HJK6/pentacle workflow.
The separately mapped private workflow's existing promotion behavior is unchanged;
this packet does not invent an audited private checkout contract.

A workflow run's short head_branch plus push event does not distinguish a tag
from a same-named branch. The public gate therefore retrieves only the selected
attempt's whole job log (GitHub serves no per-step log endpoint; gh needs
--allow-escape-sequences to print it) and parses a bounded leading prefix. It
validates the first fetch and checkout groups, which belong to the first
checkout step, against the exact candidate and refs/tags/v2-gate/<sha>. A branch-style checkout, later forged
favorable text, missing job identity, inaccessible log, or malformed evidence
cannot establish green. The existing workflow digest proves that checkout is
the first action and has no repository/ref override.

The run high-water mark survives temporarily regressed or omitted API snapshots.
A newly absent remote tag also requires complete pre-push history with no matching
historical runs; deleted/recreated tag identity is refused rather than reusing
stale green. Existing exact annotated-tag re-entry remains supported. History,
polling, job/step proof and final tag revalidation share one absolute deadline.
If the API does not expose the documented proof, the gate fails closed. No
workflow, required-check, permission or token change is made.

API assumptions and source-derived fixture references:
- https://docs.github.com/en/rest/actions/workflow-runs
- https://docs.github.com/en/rest/actions/workflow-jobs#download-step-logs-for-a-workflow-run-job
- https://raw.githubusercontent.com/actions/checkout/v4/src/input-helper.ts
- https://raw.githubusercontent.com/actions/checkout/v4/src/ref-helper.ts
- https://raw.githubusercontent.com/actions/checkout/v4/src/git-command-manager.ts

The step endpoint returns redirected plain text; gh api follows the redirect.
Its step position is zero-based (1 for the checkout step whose metadata number is
2). Attempt-specific job retrieval supplies attempt provenance; job.run_attempt
is validated when present but is not assumed to exist in the documented payload.

Synthetic validation uses only the existing fake-gh seam. The previous
queued-check/re-entry test is intentionally updated: the former two main-push
attempts become one main push after checks finish in one invocation; tag push
remains exactly once. Other existing test assertions are preserved. Baseline
replay of that journey failed on the premature main push (1 failed, 0.09s).
The new polling tests exercise latest/superseded attempts, red/missing/timeout,
late replies, tag mutation, branch/log spoofing, stale history and incomplete
evidence. No real promotion, remote tag update, main push or deploy was executed.
