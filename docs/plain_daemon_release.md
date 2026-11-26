# Plain daemon release

The authorized release operator runs these commands in a maintenance window.
Do not overlap them with another release or account-policy edit. Use a tooling
worktree at the full target SHA, not the checkout serving the daemon.

Set all four variables before running either route (there are no defaults):

- `PENTACLE_RELEASE_RUNTIME_CHECKOUT`: the daemon's runtime Git checkout.
- `PENTACLE_RELEASE_LAUNCHD_LABEL`: launchd label without `gui/<uid>/`.
- `PENTACLE_RELEASE_RUNTIME_PYTHON`: absolute runtime Python executable.
- `PENTACLE_RELEASE_DAEMON_URL`: URL for the read-only WebSocket welcome probe.

From the tooling worktree:

```sh
services/chat-stream-v2/deploy/plain_release.sh "$TARGET" --dry-run
services/chat-stream-v2/deploy/plain_release.sh "$TARGET"
```

Preflight checks the current welcome and PID, refreshes origin refs, and
requires a clean checkout, a reachable fast-forward commit, and unchanged
daemon requirements. Dry-run fetches, but never checks out or restarts.
The real run checks out the target and requires a new PID and matching welcome.
If they do not appear, it restores the prior checkout and restarts the prior.

| Exit | Meaning |
| --- | --- |
| 0 | Released and proved, or dry-run/rehearsal finished. |
| 2 | Refused without changing the serving daemon. Inspect the reason. |
| 3 | Helper route failed before the serving daemon changed. Inspect the state. |
| 4 | Forward release failed; the prior is restored and serving. |
| 5 | Needs an operator. Preserve state; inspect checkout, PID, welcome and policy. |

Use `helper_release.py` in the same deploy directory when account helper code
or its proof identity must move with the daemon. Run it with runtime Python:
`helper_release.py release "$TARGET" --work "$WORK"`, optionally with
`--hosts HOST ...`. Work must be a new private directory. It stages and proves
helpers, saves the policy bytes and mode, then rewrites only selected executor
script paths. On put-back it restores policy before restarting the prior.
Helper `--dry-run` stops before policy rewrite but leaves inert staged copies.

For an inert local rehearsal use `rehearse "$TARGET" --work "$WORK" --hosts HOST`
with `PENTACLE_RELEASE_LOCAL_HOST` set to that local machine's configured ID.
For authorized recovery use `restore-policy --work "$WORK"` or
`unstage --work "$WORK"`. Unstage removes only copies owned by this run and
not referenced by current policy, and records each removal in `state.json`.
Do not discard the run directory during review or recovery.

After helper success, verify current pin proofs, an account-bound spawn on
the local and a satellite host, and a delivered tell before lifting the hold.
