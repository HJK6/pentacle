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

Public writes go through the designated release relay. Keep the applicable local
gates, independent review and public-content checks before publishing the exact
candidate branch and opening its pull request. The required GitHub Actions
context remains `checks` (app ID `15368`); branch protection must not be relaxed.

The release owner records the reviewed PR head SHA, the hosted run's matching
head SHA, and the actual checkout SHA/tree. `Public checks` runs on pull requests
and pushes to `main`. Its four isolated partitions must all succeed, including
their required steps and source-integrity guards. The final `checks` aggregate
fails unless every expected partition succeeds; a green label from an unrelated
head or incomplete run is insufficient. Retain the per-job summaries and review
failure/cancellation evidence before retrying.

Merge only that reviewed head through the protected PR route, using the existing
expected-head guard, for example:

```sh
gh pr merge <PR> --merge --match-head-commit <reviewed-head-sha>
```

A changed head requires renewed evidence for the changed inputs. Read back the
PR's head, merged state and `merge_commit_sha`, then remote `main` and its required
checks. Record the reviewed head and actual merged SHA separately when a merge
commit differs. Check source/tree identity when GitHub tests a synthetic PR merge.
Preserve accepted evidence only for unchanged source, harness and configuration.
Local release gates, changelog review, deployment windows, installed readbacks and
rollback remain the release owner's obligations; a source merge does not deploy.

`tools/merge_gate.py promote` is retired and refuses before any external command.
It is not an alternate landing route. The retained `verify-tag` command verifies
existing legacy tags; creating a new promotion tag is not a prerequisite for the
protected PR route. Private exception projections retain their source-to-public
mapping receipts.

### Release notes

A PR with a user-visible change adds a bullet under `## Unreleased` in
`CHANGELOG.md`. Before a release candidate's `Public checks` run, the release
owner cuts the section on that branch: rename `## Unreleased` to the UTC date
heading (`## YYYY-MM-DD`, with ` (2)` for a later cut that day), then add a fresh
empty `## Unreleased` above it. Use `Internal changes only.` when the release has
no user-visible change. The release owner and relay verify that the changelog
exists, changed from the base, is well formed and has no uncut release bullets;
record the dated heading and candidate SHA with the release evidence. These
checks remain on the PR release route; the retired promotion tool does not
perform them.

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

A release that adds daemon frames or snapshot fields moves the web host and the
daemon as a pair. An older web host can throw on a newer daemon's frames and exit,
and a restart loop follows; a newer web host tolerates an older daemon. So
upgrade the web host first, then the daemon, and roll back in the opposite order:
daemon first, then web host. Never leave the older web host running against the
newer daemon; if one side must be rolled back alone, stop the web host until the
pair matches.

Historical private exclusions are reviewed by content; ordinary source and portable tests move through the same public gates. See [Public source boundary](public_boundary.md) for the shipped batch and retained private inputs.

### Historical TH-H5 promotion evidence

TH-H5's exact-tag checkout, polling, CAS and deadline evidence describes the
retired promotion path. Its [synthetic fixture manifest](../services/chat-stream-v2/tests/fixtures/th_h5/MANIFEST.json)
and tag log are retained as historical evidence. They do not establish current
PR checks or authorize a tag/main mutation. Current landing and head/readback
requirements are in [Landing on main](#landing-on-main).
