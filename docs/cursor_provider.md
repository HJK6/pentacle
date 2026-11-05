# Cursor CLI

Cursor seats use Pentacle's plain terminal REPL over Cursor's print-mode API.
The existing New Chat picker advertises **Cursor** / **Cursor Auto**, or launch
one with:

```sh
agent-orch spawn --provider cursor --model auto --effort default
```

Only local seats, the default signed-in Cursor account, and `auto/default` are
supported. Remote launches return `cursor_remote_unsupported`; named accounts
return `cursor_named_accounts_unsupported`. Public `--resume` remains Claude-only.
An unknown model returns the shared `spawn_model_unsupported` code; unsupported
effort returns `spawn_effort_unsupported`. Legacy mobile catalogs omit Cursor.
No Settings sign-in control is provided.

## Install, sign in, and pin

Install Cursor CLI using Cursor's installer, then run `agent login` and verify
`agent status --format json` reports `isAuthenticated: true`. With
`NO_OPEN_BROWSER=1 agent login`, the CLI prints a `cursor.com/loginDeepControl`
URL for browser approval. Treat that challenge URL as temporary authorization
material. A Google browser challenge needs the account owner's normal approval;
never put passwords, cookies, tokens, or authorization URLs in diagnostics.

Pentacle supports release `2026.10.22-27dcf5f`. Copy the **complete** installer
release directory (launcher, bundled Node, JavaScript chunks, workers and native
libraries), preserving its layout, to:

```text
~/.local/share/pentacle/cursor/releases/2026.10.22-27dcf5f/
```

Record a tree digest before activation. Configure the local machine entry in
`machines.json` with `cursor_agent_bin` pointing to the absolute `cursor-agent`
launcher inside that copy, and `cursor_version` set to `2026.10.22-27dcf5f`.
`cursor_repl` defaults to the shipped `cursor_repl.py` beside the daemon modules.
The daemon also accepts `--cursor-agent-bin`, `--cursor-version`, and
`--cursor-repl` overrides. All admission probes and every turn use that same
resolved launcher.

The original `agent` symlink remains the compatibility default when no explicit
pin is configured. That default carries a deferred update risk: deploy a copied
release and configure its absolute path before relying on release isolation.
The installer updater writes to its own `~/.local/share/cursor-agent/versions`
tree and replaces `~/.local/bin` symlinks. It does not select the copied release.
Turns additionally pass the CLI's `--disable-auto-update` switch. Admission
compares `--version` to the configured pin and refuses `cursor_version_mismatch`
when they differ. Manual upgrades copy and verify a new complete tree, update
both configuration values and restart the daemon. Roll back by restoring the
previous values and restarting. Do not patch vendor code in a release pin.

Admission runs off the daemon event loop. Version and JSON status probes each
have a 15-second bound; chat creation runs in a pseudo-TTY with a 20-second bound.
Missing executables return `provider_executable_missing`; unreadable or timed-out
status returns `cursor_status_unreadable`; a signed-out account returns
`cursor_not_authenticated`; failed, timed-out or non-UUID chat creation returns
`cursor_chat_create_failed`. No model request is made during admission.

## Terminal and submission contract

The pane runs the shipped Python REPL, preserving the standard seat identity and
`agent-orch` environment. A bracketed paste plus Enter is one message, including
embedded newlines. Plain typed lines submit on Enter. Empty messages are ignored.
Each admitted message immediately receives exactly one numbered receipt:

```text
» #1 Read README.md
▶ #1
│ provider output
─ #1 done 1200 ms
›
```

Input received while busy is queued and receives its receipt immediately.
`▶ #n` marks a queued turn starting; it is never a new admission. Submission
proof requires a receipt number greater than the pre-paste high-water mark and
a matching key: the trimmed message's first nonempty line, whitespace normalized,
limited to 120 characters, with trailing display whitespace removed. This handles
repeated messages, long lines and leading blank lines. Provider output and drafts
are framed separately so they cannot forge admission receipts.

Ctrl-C interrupts the running child's process group (TERM, then KILL after five
seconds), records an interruption notice, and continues queued turns. Ctrl-C
at idle keeps the seat open. `/exit` and EOF stop the child and close the REPL;
other slash-prefixed text is an ordinary prompt. Each turn has a 20-minute bound.
The fixed turn command includes `--trust --force --workspace`, the existing chat's
`--resume`, stream JSON, partial output, `--model auto`, and disabled auto-update.
Cursor's sandbox default is unchanged. The native Cursor TUI is separate; open
`agent --resume=<chat_id>` outside the seat if needed.

## Identity and ingest

Admission creates a native chat UUID before launching the pane. A different
Pentacle session UUID names the transcript. The launch records both identities,
the canonical seat cwd, the Python executable, the shipped REPL script and the
exact transcript path. The REPL exclusively creates
`<seat cwd>/.cursor/sessions/<session UUID>.jsonl`, mode 0600, writes its identity
header first, and fsyncs every appended JSON line. It never rewrites a file.
Ingest requires the recorded Python/REPL pair at the pane PID, the expected
header session/chat UUIDs, and the exact launch-bound regular file owned by the
daemon user with mode 0600 and no symlink component below the seat cwd. The seat
cwd may differ from the machine's default cwd.

Every turn resumes the pre-created chat. An unexpected `system/init.session_id`
produces one identity-mismatch notice, stops the child, and refuses further
prompts; close the seat rather than rebinding its chat.

The normalizer emits user text, each assistant model flush, the final assistant
segment unless it repeats the last flush, tool calls/results and bounded notices.
Token deltas render in the pane; thinking records remain transcript-only.
Tool inputs contain bounded paths/commands; tool results contain at most 20 lines
of summary. Event identities use the session UUID and absolute source line
number, allowing restart replay without duplicate events. Unknown or malformed
records are skipped. Plan refusal uses a bounded class notice and never copies
provider stderr into telemetry. Result usage stays in the raw transcript;
Cursor usage accounting is not implemented.

## Verification

`tests/test_cursor_provider.py` uses fake executables and real owned tmux panes
for framing, queued receipts, interrupts, child cleanup, identity and ingest
replay. Its sanitized `cursor_auto.ndjson` and `cursor_tool.ndjson` fixtures retain
the observed stream shapes. No model requests are needed for these tests.
Real acceptance uses an explicitly budgeted sequence: first-turn continuity,
read-tool events, a multiline interrupted turn and recovery, at most four prompts.
Stop immediately on plan or quota errors. Google sign-in and the complete copied
release's updater isolation are separate deployment readbacks, never inferred
from source tests.
