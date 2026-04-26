# Pentacle Desktop Sync

Pentacle desktop updates are delivered the same way shared-memory updates are delivered:

1. A push to `main` triggers `.github/workflows/desktop-sync.yml`.
2. GitHub sends a machine-targeted SQS message to each Triforce sync queue.
3. The local orchestrator on each machine consumes only its own queue.
4. The orchestrator runs `scripts/sync-desktop-update.js` in that machine's Pentacle repo.
5. The local script safely fast-forwards, installs dependencies when needed, verifies the UI code, and restarts Pentacle using local machine rules.

The message body is:

```json
{
  "source": "pentacle_desktop_sync",
  "repo": "pentacle",
  "branch": "main",
  "commit": "<git sha>",
  "sender_machine": "merlin",
  "target_machine": "bartimaeus",
  "timestamp": "2026-04-25T00:00:00Z",
  "summary": "GitHub push <sha> to HJK6/pentacle"
}
```

## Local Command

Dry-run a message:

```bash
npm run sync:desktop:dry-run -- --commit "$(git rev-parse HEAD)" --target bartimaeus --sender merlin
```

Apply a message from JSON:

```bash
npm run sync:desktop -- --message-file /path/to/message.json
```

The sync command writes local status under `.pentacle-sync/`. Dirty worktrees or restart failures produce `.pentacle-sync/pending/*.json` so the SQS message can be acknowledged without losing the required follow-up.

## Restart Rules

- Merlin and Bartimaeus default to `npm run deploy`, which rebuilds and relaunches `/Applications/Pentacle.app`.
- Amaterasu defaults to relaunching the Windows Electron dev app from `C:\Users\vamsh\repos\pentacle` with `C:\nvm4w\nodejs\npm.cmd start`.
- Any machine can override restart behavior with `PENTACLE_SYNC_RESTART_COMMAND`.
