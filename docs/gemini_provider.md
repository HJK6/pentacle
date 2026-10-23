# Optional Gemini CLI provider

Gemini support targets the interactive Gemini CLI v0.65.0 recording format.
Validation in this repository uses synthetic recordings, contributor-supplied
pane snapshots and a fake executable. We have not verified login, real model
availability, real prompt submission or a real Gemini session end to end.

Install and authenticate Gemini CLI separately using its
[official setup documentation](https://geminicli.com/docs/get-started/installation/).
Pentacle does not install it, provision an account or make a paid request during
setup or tests. Existing Claude/Codex settings and the default provider remain
unchanged. Gemini is launched only when explicitly selected.

For a local daemon, `gemini_bin` in the local machine entry can select an absolute
executable; otherwise `gemini` resolves through the daemon PATH. The daemon also
accepts `--gemini-bin`. Use the catalog's Gemini choice in New Chat, a daemon
Gemini tuple request, or an explicit CLI request:

```sh
agent-orch spawn --provider gemini --model gemini-3.1-pro-preview --effort default
```

The catalog includes `gemini-3.1-pro-preview`, `gemini-3.8-flash` and
`gemini-3.5-flash-lite`, matching constants in the
[tagged upstream model definitions](https://github.com/google-gemini/gemini-cli/blob/v0.65.0/packages/core/src/config/models.ts).
Their availability depends on the user's account. Gemini has one Pentacle effort
value, `default`: Gemini CLI manages thinking itself. Pentacle passes no effort
flag and makes no claim that `low`, `high` or other providers' effort values map
to Gemini. Such Gemini requests are rejected. The bootstrap helper accepts an
explicit Gemini tuple; its existing configuration/readback controls still apply.

Deployment-owned two-provider `spawn_defaults.json` policies remain valid and
keep their previous advertised provider set. Add a Gemini provider row containing
`{"model":"gemini-3.1-pro-preview","effort":"default"}` to advertise Gemini in
that policy. The shipped public policy includes this optional provider row.
Catalog entries alone do not launch a process or choose a new default provider.

Interactive stdout stays attached to the terminal. A helper polls native
`$HOME/.gemini/tmp/<project>/chats/session-*-<shortId>.jsonl` recordings at most
240 times, at half-second intervals, and stops within one poll after the provider
exits. It reads the full session id from each candidate header and links only a
unique match to `<machine.cwd>/.gemini/sessions/<session-id>.jsonl`. Prefix decoys
are ignored; ambiguous matches leave the transcript unbound. A missing executable
starts no helper. Stable transcript links must resolve to native recordings in
the local user's `.gemini/tmp` tree. Ingest checks the launch-owned identity before
reading content and rejects foreign or replaced recordings.

The normalizer reads user/assistant text, inline tool calls/results, late
`$patch` tool results, and error/info notices. Same-id snapshot replay is deduped;
CLI-injected `<session_context>` does not open a user turn. This follows the
[tagged recording types](https://github.com/google-gemini/gemini-cli/blob/v0.65.0/packages/core/src/services/chatRecordingTypes.ts)
and [recording implementation](https://github.com/google-gemini/gemini-cli/blob/v0.65.0/packages/core/src/services/chatRecordingService.ts).
Malformed and unknown records are skipped. Conversation rewinds, removals and
edits do not rewrite Pentacle's immutable event history. Token fields exist in
upstream recordings, but Gemini usage accounting is not implemented here.

Gemini submission proof requires a new matching prompt echo and vetoes an active
draft. Wrapped echoes are supported; unrelated history changes and API-error text
alone prove no submission. Gemini native queue detection is unsupported. The TUI
predicates describe the supplied snapshots; a future CLI layout can require new
fixtures and predicate updates.

Gemini native recording ingest is local-daemon only. Remote Gemini tuple launches
are refused until satellite support exists. Named subscription-account bindings
are also unsupported and fail closed under managed account policy; there is no
fallback into a Claude/Codex account. The launch-plan seam supports Gemini resume
only with an explicit native recording whose header matches the supplied UUID,
including a distinct resume directory. The public `agent-orch --resume` interface
remains Claude-only. No runtime activation is implied by source merge.
