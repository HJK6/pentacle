# Repository workflow

This public repository is a **release line**, not the development line. Day-to-day
development happens in a private repository whose tree is this tree plus a
private overlay; releases are published here as squash commits. The rules below
keep private material out of this repository and keep the two lines mergeable.

## Two lines, one tree

- The private line's `main` carries a merge commit whose second parent is this
  repository's `main`, so bringing public changes back into private is an
  ordinary `git merge` of the public `main`.
- Every path that exists only in the private line is listed in a checked-in
  exclude list there. A publish takes `git diff <last publish tag>..<release>`
  restricted to the non-excluded paths, applies it to a branch off public
  `main`, strips the documented overlay (private-only `package.json` scripts and
  packaging block), and runs `python3 scripts/check_public_residue.py` on the
  result before anything is pushed.
- Per-deployment settings never enter this tree: host boot limits live in
  `services/_shared/spawn_defaults.local.json` next to the shipped file, and
  private web-host profiles in gitignored `configs/<name>.local.js`.

## Landing on `main`

`main` is branch-protected: push the exact commit to a branch, let the
`Public checks` workflow go green on that SHA, then fast-forward `main` with an
explicit refspec (`git push origin <sha>:refs/heads/main`) and read it back
with `git ls-remote`. Every landing is tagged on both lines when it is a
publish. External contributions arrive as pull requests against `main` and are
merged back into the private line at the next publish.

For daemon releases, the public promotion helper adds an exact-run guard before
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

Install the pre-push guard once per clone with an absolute `core.hooksPath`
([developer onboarding § 8](developer_onboarding.md#8-pushing-safely)). It
refuses pushes that carry foreign history, omit the refspec, or aim a remote
named `public` at the wrong repository. A refusal is a finding to resolve before retrying.

## Hosted instances follow `main`

A persistent web host ([hosted profile instances](ARCHITECTURE.md#hosted-profile-instances))
is rebuilt and restarted on every landing: fast-forward its checkout, run
`npm run build:web`, restart the service, then read back `/login` (200),
`/api/config` without a cookie (401) and the daemon connection. Record the SHA
and PID of what is actually running; a checkout at the right commit is not a
deployment.
