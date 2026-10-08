# pushguard

A small host-level Git guard that refuses direct `git push` to public GitHub repositories (and to GitHub destinations it cannot classify) while leaving private repositories, local paths and other forges untouched. It is a local policy guard, not credential isolation: `--no-verify`, `git -c core.hooksPath=...` and API writes are not blocked.

- `pushguard-hook` - POSIX sh dispatcher installed under a hook name (default: `pre-push` only). For `pre-push` it classifies the push URL Git passes (`$2`), asks GitHub anonymously whether the repository is public (`200` = public, `401` = not public, anything else = unknown, which is refused), then runs the repository's original hook of the same name with stdin, arguments and exit status preserved.
- `pushguard-install` - `install [--hooks "N…"] [--hint TEXT] [DIR]`, `integrate REPO…`, `coverage ROOT…`, `uninstall`. Sets a global `core.hooksPath`, records every preimage, and restores all of them on `uninstall`. Repositories that set their own `core.hooksPath` must be integrated (`pushguard.originalHooksPath` keeps their hook directory in the chain).
- `tests/run.sh` - disposable-repo suite (mocked `curl`/`ssh`, fake local git transport, no network writes). `PUSHGUARD_RED=1` skips the install so the guard assertions fail.
- `tests/live-proof.sh` - host proof after installation (`live`) or a rehearsal with a temporary install (`sim`); needs `PUSHGUARD_PROOF_PUBLIC` and `PUSHGUARD_PROOF_PRIVATE` (`owner/repo`) for two anonymous read-only lookups.

Requirements: POSIX `sh`, `git` >= 2.38, `curl`, and `ssh` for alias resolution. Works with Git for Windows' bundled `sh`.
