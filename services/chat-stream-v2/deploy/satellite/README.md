# chat_streamd v2 ingest satellite — install

The satellite (`../../satellite.py`) uses one local tmux discovery pass to tail
Claude transcripts and open Codex rollout JSONL files, then pushes normalized
events and Claude/Codex working-state observations to coordinator's existing
`event.push` verb. Chat reaches `session_event_tail` (and mobile) live, while
working edges and heartbeats reach the existing `RemotePresence` tracker
without a second watcher. It is stateless (in-memory offsets and pane
hysteresis only), supervised, and self-updating to the SHA coordinator pins. See
`satellite.py`'s module docstring for the design.

## 0. Shared push secret (both ends)

`event.push` fails closed: coordinator refuses every push unless a secret is configured
on BOTH ends and they match.

- **coordinator** daemon env: `PENTACLE_EVENT_PUSH_SECRET=<secret>` (or kv
  `event_push.target_sha`'s sibling key `event_push.secret`). Set it in coordinator's
  chat_streamd launchd plist / deploy env.
- **Satellite host**: write `~/.config/pentacle/satellite.env` (mode 600):

  ```
  PENTACLE_EVENT_PUSH_SECRET=<the same secret>
  ```

Generate once with `openssl rand -hex 32` and share it across the fleet the same
way other fleet secrets are provisioned.

## 1. Dedicated checkout (do NOT use a dev worktree)

Auto-update runs `git fetch origin && git checkout --detach <sha>` in the
satellite's checkout. Give it its own clone so it never disturbs in-flight work:

```
# linux-workstation (WSL)
git clone git@github.com:example-org/pentacle.git ~/repos/pentacle-satellite
# workstation (macOS)
git clone git@github.com:example-org/pentacle.git ~/repos/pentacle-satellite
```

`__CHECKOUT__` below is that path (e.g. `/home/example/repos/pentacle-satellite` on
linux-workstation, `/Users/example/repos/pentacle-satellite` on workstation).

## 2a. linux-workstation / WSL — systemd-user

```
sed "s#__CHECKOUT__#$HOME/repos/pentacle-satellite#g" \
  pentacle-satellite.service > ~/.config/systemd/user/pentacle-satellite.service
loginctl enable-linger "$USER"          # run without an interactive login
systemctl --user daemon-reload
systemctl --user enable --now pentacle-satellite
systemctl --user status pentacle-satellite     # verify active
journalctl --user -u pentacle-satellite -f     # tail logs
```

## 2b. workstation / macOS — launchd

```
sed "s#__CHECKOUT__#$HOME/repos/pentacle-satellite#g" \
  com.pentacle.satellite.plist > ~/Library/LaunchAgents/com.pentacle.satellite.plist
launchctl unload  ~/Library/LaunchAgents/com.pentacle.satellite.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.pentacle.satellite.plist
tail -f /tmp/pentacle-satellite.log            # verify connect + pushes
```

## 3. Verify

`linux-workstation:v2-*` / `workstation:v2-*` rows should appear in coordinator's
`session_event_tail` within a cadence or two (default 1 s), including an active
Codex stream whose rollout file is still open in its child process. For every
surfaceable Claude or Codex pane, the same pass emits a working edge and repeats
the current six-field observation at most every 5 s, even when the transcript is
quiet. coordinator uses the pushed state as primary and resumes its existing central
SSH/tmux capture only after two missed 5 s heartbeats (10 s).

The observation metadata is not chat history. Its fields are `host`, `stream_id`,
`session_name`, `provider`, `working`, and `working_label`; the daemon supplies
the receive time used by the existing working-state tracker. Confirm the exact
end-to-end chat path with `request_stream_events` against prod coordinator for a live
session on that host, and confirm the live row's `working` / `working_label`
overlay through the existing `list_sessions` read path.
Confirm the exact end-to-end path the app uses with `request_stream_events`
against prod coordinator for a live Claude and Codex session on that host, and confirm
the live row's `working` / `working_label` overlay through the existing
`list_sessions` read path.

## Knobs / kill switches (env, `PENTACLE_SATELLITE_*`)

- `HOST` — fleet name; MUST match the registry host (`linux-workstation`/`workstation`).
- WebSocket endpoint: set the full environment key `PENTACLE_SATELLITE_BART_WS=ws://127.0.0.1:7791` for a local daemon, or use your configured daemon endpoint. This legacy key is part of the satellite API.
- `SESSION_GLOB` — tmux session-name filter, default `v2-*`.
- `HISTORY_BYTES` — first-bind history horizon (default 1 MiB): on initial bind
  the tail starts within the last this-many bytes of a transcript so live turns
  reach mobile ahead of the backlog. `-1` replays the whole file.
- `DISABLE=1` — kill switch: stay connected, push nothing.
- `NO_AUTOUPDATE=1` — never git-checkout/exec-restart (stay on the running SHA;
  coordinator still accepts pushes flagged-stale and alerts).
- `INTERVAL_S`, `MAX_EVENTS`, `MAX_READ_BYTES`, `UPDATE_MIN_INTERVAL_S` — existing loop knobs.

Working freshness does not add a knob: the local scan remains one pass and the
working heartbeat uses the daemon's existing five-second tracker constant.

coordinator-side kill switch: `--disable-event-push-ingest` makes `event.push` return
`ingest_disabled`; the satellite backs off and keeps its offsets (no loss).

## Serialized deploy-window pin step

Only in a Nexus-authorized deploy window, after the new coordinator daemon has passed
its health readbacks, stage the exact deployed 40-character SHA and retain the
JSON output as the durable readback:

```
/opt/homebrew/opt/python@3.13/bin/python3.13 services/chat-stream-v2/tools/event_push_pin.py \
  --db /path/to/sessions.db --stage <exact-40-character-deployed-sha>
```

The command records the preceding pin before changing the target, writes the
new value, and reads it back. It is not a deployer: it does not fetch, check
out, restart, or contact satellites.

## Rollback

In the same Nexus-authorized serialized window, restore the captured prior pin
with a readback before rolling coordinator back to its prior accepted SHA:

```
/opt/homebrew/opt/python@3.13/bin/python3.13 services/chat-stream-v2/tools/event_push_pin.py \
  --db /path/to/sessions.db --rollback
```

Then verify the satellites return to it. The optional observation field is
non-durable and requires no migration.
Confirm `hello`, `list_sessions`, and `request_stream_events` still work and
record the return to the known central-capture freshness behavior.
