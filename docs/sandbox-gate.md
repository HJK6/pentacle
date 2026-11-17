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


## Native bootstrap amendment (lanes-sandbox-gate-2)

The generated profile now places `(import "/System/Library/Sandbox/Profiles/bsd.sb")`
immediately after `(deny default)`, followed by the narrow harness rules. Every
literal transitive imported SBPL file, including system.sb, is read and hashed;
unresolved, unreadable or nonliteral imports are INCOMPLETE. Imported policy
hashes are checked again before launching and when validating native permission.

This is **not** a policy of no home-path access: bsd.sb/system.sb grant global
file metadata, a `.CFUserTextEncoding` suffix exception, selected system data
and `/dev/fd`. Ordinary home file contents, protected database and credential
contents, and unowned temporary content remain denied. The preflight adds
harness-created credential-named and database-named sentinel files in a fresh
directory within the real home tree. Every opener must be denied their contents;
unsandboxed controls must read them. Only these owned files/directories are
created and removed; no real credential or database is opened.

The read-only dependency inventory uses `otool -L` and load-command inspection
recursively for probe/preflight executables, each real interpreter, Python
extension modules (including dynamically loaded `_sqlite3`), and discovered
node addons. `@rpath`, `@loader_path` and `@executable_path` must resolve to exact
files; symlink lookup metadata is retained separately. No formula/Cellar parent
or Homebrew-wide rule is inferred. Dylibs get exact file read/map grants, not
write or directory-wide grants. Python stdlib code directories are explicit
narrow runtime read roots discovered from the selected interpreter. Cycles
terminate; excessive depth, drift or an unresolved/unreadable dependency makes
the attempt INCOMPLETE. Only missing absolute system install names under `/usr/lib/`,
`/System/Library/` or `/Library/Apple/System/Library/` may be recorded as
`shared_cache_provided` rather than granted. Raw `..` components, symlink
escapes, existing paths (including unreadable files and dangling links),
non-system misses and token-expanded misses never qualify. This exception
requires readable main `dyld_shared_cache_<arch>` files in the Cryptex
`/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/`, legacy
`/System/Library/dyld/`, or `/private/var/db/dyld/` locations. All discovered
main caches are streaming-hashed (at most 16); main-file symlinks and directory
symlink escapes from the fixed OS cache locations are refused, and hashes are rechecked before
returning the closure. Numbered subcaches, maps and symbols are not main-cache
receipts. Missing/unreadable caches, hashing errors and drift are INCOMPLETE.
The closure and `sandbox-profile.json` record each accepted name and cache
path/SHA256. This establishes cache presence, not individual image membership;
fleet's native proof remains required. No name, cache path or new directory
allow is added to the profile: its grant set is unchanged.

Every launched probe/preflight/test has a realpath cwd within declared allowed
roots and `close_fds=True`, `pass_fds=()`. Only standard descriptors 0/1/2 are
inherited/remapped; subprocess-owned capture pipes are intentional. A portable
real-child test proves an explicitly inheritable parent sentinel descriptor is
closed, and rejects an outside cwd. The harness closes other descriptors before
running sandbox-exec, not after child code starts.

Native Seatbelt, imported system policy and dyld behavior remain fleet proofs.
Linux injected-runner green is not evidence that macOS can start the generated
profile or that every runtime dependency has been observed. A missing input or
failed native control stops before tests; no runtime denial expands the rules.

The explicit preflight executable inventory includes `/bin/cat`, bash, git, ps,
lsof, uname, env, true, dirname, ifconfig, awk and sleep, in addition to the
probe interpreters, node, tmux, sqlite3, sandbox-exec and forbidden-exec controls.
Known Python dlopen extensions and node addons are dependency roots, not merely
the main executables. Future/generated executables cannot be certified from a
source-only inventory; runtime denial is not proof of their dependency closure
and must not be presented as full gate readiness.

## Explicit grants added after the first native runs

Each is an exact, reviewed source edit with a unit test; none comes from a denial log.

- **Real interpreters.** Python launchers exec a real binary that otool does not show
  (the framework `Resources/Python.app/Contents/MacOS/Python`). At prepare time the
  interpreter is run unsandboxed with `-I -S -B` to print its real file and that app
  binary; both go through the same dependency closure and exact exec/read grant.
- **No xcrun shim.** `/usr/bin/python3` is a shim that loads the developer directory at
  run time. The alternate-interpreter probes use the real interpreter behind it instead
  (exact file plus closure); the shim itself is not probed or granted. The receipt lists
  `alternate_interpreters`.
- **node.** Child environments set `OPENSSL_CONF` to an empty file in the owned attempt
  root, so node needs no grant on the system OpenSSL configuration.
- **tmux pseudo-terminals.** The only device grant is read/write/ioctl on `/dev/ptmx`,
  `/dev/tty` and `^/dev/ttys[0-9]+$`. No `/dev` subpath is granted.
