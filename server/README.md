# Pentacle API Server

Stdlib-only Python HTTP server that manages tmux sessions for Pentacle.
No external dependencies — requires only Python 3.9+ and tmux.

## Endpoints

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/` | — | `{"ok":true,"pentacle_api":1}` |
| GET | `/api/sessions` | — | `{"active":[…],"trashed":[…]}` |
| POST | `/api/rename` | `{"session":"<name>","new_name":"<name>"}` | `{"ok":true}` |
| POST | `/api/trash` | `{"session":"<name>"}` | `{"ok":true}` |
| POST | `/api/restore` | `{"agent_id":"<uuid>"}` | `{"ok":true}` |
| POST | `/api/kill` | `{"session":"<name>","agent_id":"<uuid>"}` | `{"ok":true}` |
| POST | `/api/new` | `{"name":"<name>"}` (optional) | `{"ok":true}` |
| POST | `/api/cleanup` | — | `{"ok":true,"killed":[…]}` |

Active entry fields: `name`, `display_name`, `title`, `preview`, `attached`, `type`, `agent_id`, `created`, `last_activity`.  
Trashed entry fields: `name`, `display_name`, `agent_id`, `trashed_at`.

Not implemented (set feature flags to `false`): `/api/usage`, `/api/codex-usage`, `/api/bots`.

## State files / launch

State: `~/.local/state/pentacle/{agents,trash}.json` (Linux/macOS) · `%LOCALAPPDATA%\Pentacle\` (Windows).
`agents.json` maps session names → stable UUIDs. `trash.json` holds soft-deleted sessions.

`main.js` spawns `python3 server/server.py` (repo-relative path, `$HOME` fallback). Detached — survives Pentacle restarts.

## Standalone testing

```bash
# Start the server
python3 server/server.py

# In another terminal:
curl http://127.0.0.1:7777/
curl http://127.0.0.1:7777/api/sessions
tmux new -d -s test-hello
curl http://127.0.0.1:7777/api/sessions     # test-hello appears in active[]
curl -X POST http://127.0.0.1:7777/api/rename \
  -H 'Content-Type: application/json' \
  -d '{"session":"test-hello","new_name":"test-world"}'
curl -X POST http://127.0.0.1:7777/api/trash \
  -H 'Content-Type: application/json' \
  -d '{"session":"test-world"}'
curl http://127.0.0.1:7777/api/sessions     # test-world in trashed[]
```

## Options

```
python3 server.py [--port 7777] [--host 127.0.0.1]
```

Default binds to 127.0.0.1 only (localhost-only). Remote access is via SSH tunnel.
Hidden sessions: `cmdcenter`, `quad-view`. Add more via `PENTACLE_HIDDEN_PREFIXES=foo,bar`.
