# Repository workflow

Public `main` is the source of truth for development and runtime releases.
Start new branches from freshly fetched public `main` and land them here after
the applicable local gates, independent review and exact-SHA CI checks.

## Private-data exceptions

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

The tag push can start another required `Public checks` run. If branch
protection holds `main` while that check is queued, wait for its result before
re-entering the helper with the original successful branch-push run ID. On
re-entry the helper skips a redundant tag push only when the remote annotated
tag object and peel exactly match its locally validated tag; conflicting or
unreadable tag state refuses before the `main` push. Keep the pre-push guard
enabled and preserve the original tag and refusal receipts.

## Push guard

Before every public push, run `python3 scripts/check_public_residue.py`, review
the exact candidate diff for private data, endpoints, local paths and credentials,
and compare fleet-name hits with public `main` using the existing no-new-hit
policy. A new hit requires sanitization or a scoped coordinator ruling. The
residue checker detects anonymizer residue and CGNAT addresses; it does not
replace private-content review. Its optional fixed mobile synthetic profile
supports that client's existing symbolic identifiers; default web checks remain
unchanged. Mobile vendors this reviewed checker with a source hash receipt.


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
