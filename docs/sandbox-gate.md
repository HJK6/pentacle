# Exact-profile sandbox gate

The launcher adds OS process-tree confinement to the attempt-owned SQLite
registry; it does not replace or loosen that registry. Every test-starting
`gate_env.py run` target generates an SBPL file, runs its self-probes, and only
then launches builds, preflight and tests under that **same profile hash** using
`/usr/bin/sandbox-exec -f PROFILE`. Preparation alone does not start tests.

## Generated policy

The profile starts with `deny default`. Read/exec scope is the realpath-resolved
candidate checkout, prepared venv, resolved toolchain package roots and explicit
system-code directories. Writes are limited to the owned attempt root, evidence
and the owned tmux socket directory. No whole home, `.local`, `agent-workspace`,
shared cache, `/opt/homebrew` or whole temporary-directory allowance is generated.
The generator rejects broad home roots independently of the operating user.
IP networking is left enabled. AF_UNIX socket creation is permitted, but Unix
socket binding/connect is restricted to the owned socket directory. The profile
explicitly denies execution of sudo, su, launchctl, open and osascript.
No broad Mach service lookup allowance is granted.

The syscall/filter shape for Unix sockets follows the Seatbelt use in the
[Codex sandbox implementation](https://github.com/openai/codex/blob/main/codex-rs/sandboxing/src/seatbelt.rs).
This reference establishes syntax intent, **not validation of this profile**.
The generated profile has not been parsed or exercised on macOS in the author's
Linux workspace. Resolved Homebrew package roots may not include every required
dylib dependency. Tmux may require separately reviewed terminal-device access.
The strict profile does not silently grant those dependencies. Fleet must run
the exact generated profile; a dependency denial or parser failure is incomplete
and requires an explicit reviewed source correction before another proof.

## Pre-test proof

Only harmless databases created by the harness are used: an owned control and
two forbidden sibling temporary directories representing home data and unowned
temporary data. All databases are explicitly closed and the forbidden temporary
directories are removed after probing.

For each sentinel, the exact launcher tests direct venv Python, isolated venv
Python, the Homebrew and system alternate Python interpreters both with and
without `-S`, sqlite3 CLI, node, `/bin/sh -c`, and a pane in a **new owned tmux
server on an explicit socket**. Each opener also has an owned positive control.
Unsandboxed Python controls prove both forbidden sentinels are readable without
Seatbelt. A sandboxed parent and child create/reopen owned databases. Python,
node and tmux version controls must run. All five forbidden executables have
negative controls. No real production database is opened.

The probe runs while the prepared Python hook is inactive, so a Python refusal
cannot masquerade as proof of OS confinement. Once proof passes, the merge gate
activates the existing attempt registry and may permit non-Python children only
while that exact attempt/profile/receipt remains verified. Guarded Python `-S`,
direct alternate interpreters and `os.system` remain refused. The public launcher
refuses to launch subjects without a completed proof; its private probe launcher
exists solely to obtain that proof.

## Incomplete conditions and evidence

Missing sandbox-exec or any required opener, profile generation/parsing failure,
stale active Python hook, readable forbidden sentinel, failed owned/tool/cleanup
control, unexpected probe result, unreadable or changed profile, and a missing or
mismatched proof receipt all fail closed **before tests**. No toy profile or old
attempt proof is accepted.

`EVIDENCE/gate/sandbox-profile.sb` is the actual generated profile.
`sandbox-profile.json` records its SHA256, resolved rules, launcher prefix,
attempt root, opener and control outcomes, and execution mode. Unit-injected
runners are explicitly labelled `injected-test-runner`, not native OS proof.
`sqlite-guard.json` remains separate. A denial log may be retained for human
review, but the code never reads a denial log to add rules or expands allowances
from diagnostics. Rollback is to revert the harness change, not relax a rule.

## Limits

`sandbox-exec` is deprecated and macOS-specific. Unit tests on another OS prove
only deterministic generation, wrapper and fail-closed bookkeeping, not Seatbelt
confinement. This is not a defense against kernel-level escapes, malicious
same-user modification of harness artifacts, or any process the harness starts
outside the launcher. Native filesystem access inside the allowed roots is
controlled by Seatbelt, while Python SQLite continues to require attempt-owned
registered database identities. Fleet owns native proofs and the full merge
gate; neither was run in the author's workspace.
