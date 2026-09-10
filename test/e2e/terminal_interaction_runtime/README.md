# Native terminal interaction regression

Run this opt-in macOS harness with Electron 44 after changing terminal input,
selection, scroll, attach, or clipboard handling. It uses a visible, focused
Electron window and the real OS clipboard, so run it when brief foreground
focus is available. `npm test` does not invoke it.

From the repository root, with Electron/native dependencies installed:

```sh
PENTACLE_RUNTIME_CLIPBOARD=1 node test/e2e/terminal_interaction_runtime/run.js
```

The harness snapshots every available clipboard item/type into memory before
writing a synthetic sentinel. It restores the complete snapshot only if the
clipboard still contains its last known sentinel. It never logs the previous
clipboard. A changed external clipboard aborts dependent copy/paste cells.

It creates an isolated Electron profile, uniquely named tmux server, and raw
stdin fixtures. A Node parent supervises Electron and removes its exact temporary
profile directory after the child exits, including storage files recreated
during Chromium shutdown. It changes only that server's settings and its own sessions;
it never attaches an existing user session. Normal completion, failures,
SIGINT, and SIGTERM dispose owned PTYs, kill the private server, and remove
fixture directories. Results remain in the printed output directory. Exit 0
means all cells passed; 1 means an assertion failed; 2 means setup or cleanup
failed. SIGKILL cannot run cleanup.

Configuration is through environment variables:

| Variable | Default / purpose |
| --- | --- |
| `PENTACLE_RUNTIME_ELECTRON` | Installed `electron` package executable; override to select a development Electron bundle. |
| `PENTACLE_RUNTIME_CLIPBOARD` | Must explicitly equal `1`. |
| `PENTACLE_RUNTIME_SOURCE` | Repository root; may point to a packaged `app.asar`. |
| `PENTACLE_RUNTIME_DEPENDENCIES` | `<source>/node_modules`; must contain xterm addons and node-pty matching the running Electron ABI. |
| `PENTACLE_RUNTIME_OUTPUT` | A new temporary results directory. Receives `receipt.json` with candidate/harness hashes and exact synthetic-input receipts. |
| `PENTACLE_RUNTIME_TMUX` | `tmux`, for a local fixture. |
| `PENTACLE_RUNTIME_PYTHON` | `python3`, for a local fixture. |
| `PENTACLE_RUNTIME_SSH_TARGET` | Unset for local; set to a configured SSH hostname or `user@hostname` to test remote transport. Requires batch-mode SSH access. |
| `PENTACLE_RUNTIME_SSH_PORT` | `22`. |
| `PENTACLE_RUNTIME_REMOTE_TMUX` | `tmux` on the remote host. |
| `PENTACLE_RUNTIME_REMOTE_PYTHON` | `python3` on the remote host. |

Remote mode allocates a new `/tmp/pentacle-terminal-fixture.*` directory on the
remote host, copies the owned collector/wrapper there, and removes it afterward.
Use absolute executable paths if the noninteractive SSH PATH differs.

The harness loads production preload, clipboard bridge, terminal adapter,
paste helper, and extracted terminal/wheel wiring from the specified source.
It uses real WebGL xterm, native mouse/key input, native macOS copy/paste actions,
and a raw stdin collector rather than a mocked terminal sink. It verifies:

- inherited global `mouse on`, persistent drag and double-click selection;
- Cmd+C/Cmd+V and native copy/paste, including selected scrollback;
- ordinary input and Ctrl+C, first key/Ctrl+C/Ctrl+Enter after scrolling;
- rapid live typing without redundant clear commands, and a bounded wheel burst;
- repeated attach and replacement without input reaching the prior session;
- attached-session mouse policy with global/unrelated sessions unchanged;
- two-client resizing under both global `latest` and cold `smallest`, with only
  the attached window adopting its required policy.

A receipt proves the source and runtime named in its hashes. Deployment still
requires matching installed content and identifying the live process. The
harness does not test chat composer behavior or non-macOS input conventions.

Cleanup fault injection runs without Electron or clipboard access:

```sh
node --test test/e2e/terminal_interaction_runtime/cleanup.test.js test/e2e/terminal_interaction_runtime/supervisor.test.js
```

Cleanup aggregates stage errors and still attempts independent work. If server
termination cannot be established, it preserves fixture directories and records
recovery paths/socket in the receipt. A transport failure is never treated as
proof that the remote server is absent. Native gestures wait for parsed output,
attach/control commands, and document focus before mutating the clipboard.

The supervisor writes `post-exit-cleanup.json`; a missing or mismatched child
receipt preserves the fixture for recovery. Spawn failures before a child starts
can safely remove the unused root. SIGINT/SIGTERM are forwarded to the child; the
parent waits for child closure before any removal. A stale successful receipt
from an earlier invocation cannot authorize deleting the current fixture.
