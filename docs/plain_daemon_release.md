# Plain daemon release

Use a tooling worktree at the target SHA in a maintenance window.
Do not overlap a release with another release or account-policy edit.

Set all four required variables before either route (there are no defaults):

- `PENTACLE_RELEASE_RUNTIME_CHECKOUT`: the daemon's runtime Git checkout.
- `PENTACLE_RELEASE_LAUNCHD_LABEL`: launchd label without `gui/<uid>/`.
- `PENTACLE_RELEASE_RUNTIME_PYTHON`: absolute runtime Python executable.
- `PENTACLE_RELEASE_DAEMON_URL`: URL for the read-only WebSocket welcome probe.
- `PENTACLE_RELEASE_RECORD_DIR` (optional): existing directory for release JSON records.
- `PENTACLE_RELEASE_PROBE_TARGET` (optional): stream to receive restart delivery probes.

From the tooling worktree:

```sh
services/chat-stream-v2/deploy/plain_release.sh "$TARGET" --dry-run
services/chat-stream-v2/deploy/plain_release.sh "$TARGET"
```

Preflight checks the current welcome and PID, refreshes origin refs, and
requires a clean checkout, a reachable fast-forward commit, and unchanged
daemon requirements and helper identity. Helper changes require the helper route.
Dry-run fetches, but never checks out or restarts.
The real run checks out the target and requires a new PID and matching welcome.
If they do not appear, it restores the prior checkout and restarts the prior.
Each real run after preflight writes one JSON record with outcome, exit code,
UTC preflight/stop/serving times, commits, PIDs, boot ID and probe verdict.
If the record directory is unset or unwritable, the script prints a warning.
A missing boot marker is recorded as null. The probe runs only when configured;
It sends 10 tells; its JSONL evidence stays beside the record. A failed probe
does not put back a serving release.
Never export `PENTACLE_RELEASE_PUTBACK_WORK` in a shell.

| Exit | Meaning |
| --- | --- |
| 0 | Released and proved, or dry-run/rehearsal finished. |
| 2 | Refused without changing the serving daemon. Inspect the reason. |
| 3 | Helper route failed before the serving daemon changed. Inspect the state. |
| 4 | Forward release failed; the prior is restored and serving. |
| 5 | Needs an operator. Preserve state; inspect checkout, PID, welcome and policy. |
| 6 | Released and serving, but the delivery probe failed. |

Use `helper_release.py` in the same deploy directory when account helper code
or its proof identity must move with the daemon. Run it with runtime Python:
`helper_release.py release "$TARGET" --work "$WORK"`, optionally with
`--hosts HOST ...`. Work must be a new private directory. It stages and proves
helpers, saves the policy bytes and mode, then rewrites only selected executor
script paths. On put-back it restores policy before restarting the prior.
Helper `--dry-run` stops before policy rewrite but leaves inert staged copies.

For rehearsal use `rehearse "$TARGET" --work "$WORK" --hosts HOST` with
`PENTACLE_RELEASE_LOCAL_HOST` set to that local machine's configured ID.
For authorized recovery use `restore-policy --work "$WORK"` or
`unstage --work "$WORK"`. Unstage removes only copies owned by this run and
not referenced by current policy, and records each removal in `state.json`.
Do not discard the run directory during review or recovery.
After helper success, verify current pin proofs, an account-bound spawn on
the local and a satellite host, and a delivered tell before lifting the hold.
