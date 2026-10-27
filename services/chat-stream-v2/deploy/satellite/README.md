# chat_streamd v2 ingest satellite — install

The satellite (`../../satellite.py`) uses one local tmux discovery pass to tail
Claude transcripts and open Codex rollout JSONL files, then pushes normalized
events and Claude/Codex working-state observations to coordinator's existing
`event.push` verb. Chat reaches `session_event_tail` (and mobile) live, while
working edges and heartbeats reach the existing `RemotePresence` tracker
without a second watcher. It retains atomic native-source/cursor metadata,
is supervised, and updates to the exact SHA coordinator pins. See
`satellite.py`'s module docstring for the design.

## 0. Shared push secret (both ends)

`event.push` fails closed: coordinator refuses every push unless a secret is configured
on BOTH ends and they match.

- **coordinator** daemon env: `PENTACLE_EVENT_PUSH_SECRET=<secret>` (or kv
  `event_push.target_sha`'s sibling key `event_push.secret`). Set it in coordinator's
  chat_streamd launchd plist / deploy env.
- **Satellite host**: write `~/.config/pentacle/satellite.env` (mode 600):

  ```
  PENTACLE_SATELLITE_HOST=workstation
  PENTACLE_SATELLITE_WS=ws://coordinator.example:7791
  PENTACLE_EVENT_PUSH_SECRET=<the same secret>
  PENTACLE_EVENT_PUSH_HOST_SECRET=<this host's per-host HMAC secret>
  ```

Replace `workstation` with this satellite's registered machine name and
`coordinator.example` with the reachable daemon address before installation.

Generate the bearer once with `openssl rand -hex 32` and share it across the
fleet. Generate a distinct host proof secret for each satellite and provision
the matching coordinator key `event_push.host_secret.<host>`; the host proof
secret must not be shared across hosts.

## 1. Dedicated checkout (do NOT use a dev worktree)

Auto-update runs `git fetch origin && git checkout --detach <sha>` in the
satellite's checkout. Give it its own clone so it never disturbs in-flight work:

```
# linux-workstation (WSL)
git clone https://github.com/HJK6/pentacle.git ~/repos/pentacle-satellite
# workstation (macOS)
git clone https://github.com/HJK6/pentacle.git ~/repos/pentacle-satellite
```

Detach the dedicated clone at the accepted source and prepare its own venv
using the exact service Python (supported service tuples: Python 3.12 and 3.13):

```sh
git -C ~/repos/pentacle-satellite checkout --detach <accepted-full-sha>
python3 -m venv ~/repos/pentacle-satellite/.venv
~/repos/pentacle-satellite/.venv/bin/python -m pip install -r \
  ~/repos/pentacle-satellite/services/chat-stream-v2/requirements.txt
```

Prepare dependencies before staging a pin. Do not share or symlink this venv
with account/usage helpers, and keep those helpers on their existing checkout.
The templates execute `__CHECKOUT__/.venv/bin/python` and the script from that
same checkout. Merely changing the CHECKOUT environment variable is insufficient.
The updater requires an owned clean detached clone, matching executed source,
the configured origin (default public repository), and the dedicated venv. It
fetches the exact full SHA, preflights candidate imports and the websockets pin
with that interpreter, and restores the previous checkout on pre-exec failure.
It never installs dependencies in the tail loop. A source restoration is not
proof that the supervisor booted successfully.

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

## Restart and closed-session recovery

The default metadata file is `~/.local/state/pentacle/satellite_chat.json` (mode
600, atomic replace plus fsync). Preserve it and the original transcript files
through updates and rollback. A replacement inode/path, truncated or changed
retained bytes, corrupt checkpoint, missing file, or missing original binding
reports a recovery gap. Unrelated live streams continue. Corrupt metadata is
preserved and prevents an unproved cold-tail seek; it is never overwritten to
manufacture a successful recovery.

Before interruption, a host-authenticated push must acquire a daemon-issued
checkpoint against the open pane/provider/native session/file identity and
original generation. Bootstrap and verify these live bindings before enabling
autoupdate. Never infer a closed source from a name, timestamp or reused PID.
An installed-old satellite without retained checkpoints cannot retrospectively
prove that history; report the gap explicitly.

On reconstruction, retain already-discovered sources even when their panes
have closed. Persist a fixed complete-record EOF/digest before sending, then
replay from the last acknowledged cursor. ACK/checkpoint interruption can
resend records; existing SQLite transcript identity dedupes them. Each source's
range/order/identity check and cursor advance share its append transaction.
Retries cannot widen the range. Incomplete trailing bytes stay unacknowledged
and non-green. Reusing one native file across generations cannot prove which
generation owns an undrained tail and is refused.

The original live-issued source proof remains stable through closure and live
resumption. Each recovery range has its own immutable ID, bounds and prefix
digest. Only after its predecessor completes may the next range begin, exactly
at that predecessor's durable end, after revalidating the retained native/file
identity and prefix. Recording another bounded range after closure does not
issue a new identity. ACK retries keep the old range exact; the satellite
atomically records completion before starting the next range. Live bytes beyond
a completed range replay through the existing dedupe sink when needed to keep
that sequence contiguous, including across crashes and lost ACKs.

Historical append changes only the original conversation history and recovery
metadata. It cannot reopen a seat, refresh activity/working, change provider/account
or routing state, affect capacity retry, mirror an assistant turn, or broadcast
a new live turn. Normal late live output retains its lifecycle fences. The
existing `request_stream_events` default reads the current lifecycle; an
operator-authenticated request may include `generation` to read a recorded
original checkpoint lifecycle after a same-name reopen. Scoped/seat callers
cannot use that historical selector. The selector resolves the recorded
original lifecycle; the default keeps the existing current-lifecycle fence.
Generation checkpoint metadata follows the existing 30-day closed-history
retention; missing/pruned proof remains a recovery gap.

## Knobs / kill switches (env, `PENTACLE_SATELLITE_*`)

- `PENTACLE_SATELLITE_HOST` — fleet name; MUST match the registry host (`linux-workstation`/`workstation`).
- `PENTACLE_SATELLITE_WS` — reachable coordinator WebSocket endpoint. When unset, the default is `ws://127.0.0.1:7791`.
- `PENTACLE_SATELLITE_SESSION_GLOB` — tmux session-name filter, default `v2-*`.
- `PENTACLE_SATELLITE_HISTORY_BYTES` — first-bind history horizon (default 1 MiB): on initial bind
  the tail starts within the last this-many bytes of a transcript so live turns
  reach mobile ahead of the backlog. `-1` replays the whole file.
- `PENTACLE_SATELLITE_DISABLE=1` — kill switch: stay connected, push nothing.
- `PENTACLE_SATELLITE_NO_AUTOUPDATE=1` — never git-checkout/exec-restart (stay on the running SHA;
  a configured exact pin rejects mismatched pushes and requests an update).
- `PENTACLE_EVENT_PUSH_HOST_SECRET` — this satellite's per-host HMAC secret;
  usage-bearing pushes sign `event.push.v1\0<host>\0<satellite-sha>\0<satellite-pid>`.
- `PENTACLE_SATELLITE_CHAT_CHECKPOINT_PATH` — override the metadata file for an owned service/test instance.
- `PENTACLE_SATELLITE_UPDATE_ORIGIN` — expected exact origin URL; configure only the authoritative release repository.
- `PENTACLE_SATELLITE_INTERVAL_S`, `PENTACLE_SATELLITE_MAX_EVENTS`, `PENTACLE_SATELLITE_MAX_READ_BYTES`, `PENTACLE_SATELLITE_UPDATE_MIN_INTERVAL_S` — existing loop knobs.

Working freshness does not add a knob: the local scan remains one pass and the
working heartbeat uses the daemon's existing five-second tracker constant.

coordinator-side kill switch: `--disable-event-push-ingest` makes `event.push` return
`ingest_disabled`; the satellite backs off and keeps its offsets (no loss).

## Serialized deploy-window pin step

Only in a Nexus-authorized deploy window, after the new coordinator daemon has passed
its health readbacks, stage the exact deployed 40-character SHA and retain the
JSON output as the durable readback:

Before enforcement, verify that every participating satellite's configured checkout
fetches the release repository, can resolve the accepted public release SHA, has
the required dependencies, and either already runs the target or permits auto-update.
If auto-update is disabled, install and restart the peer explicitly in the serialized
window before staging the pin. Record each running SHA and PID,
checkout state and rollback preimage. A moved checkout HEAD does not establish that
its satellite process restarted. The target is the public release SHA used by the
daemon and peers; a private integration SHA is not a satellite release.

```
/opt/homebrew/opt/python@3.13/bin/python3.13 services/chat-stream-v2/tools/event_push_pin.py \
  --db /path/to/sessions.db --stage <exact-40-character-deployed-sha> \
  --gate-evidence /path/to/public-release-merge-gate.json \
  --satellite-preflight /path/to/satellite-preflight.json \
  --url ws://coordinator.example:7791
```

The command uses the deployer's canonical local merge-evidence validator: the
artifact must name the exact public release SHA, clean source before and after,
and passing unit and smoke tiers. Generate it on the actual public candidate;
never relabel private-candidate evidence. Missing or invalid evidence refuses
before the pin changes. Remote workflow tags are not used by the pin writer.
The command retains the preceding pin once; repeating the same target resumes
observation without replacing that rollback preimage. It waits up to 420 seconds
for required participants' fresh target incarnation and admitted telemetry,
runs the existing URL/HOST Codex promptless send/reply/history canary for each
ready participant, then observes useful accepted events and zero pending
recovery/gaps. PID and HEAD are insufficient; exec preserves PID. The canary
keeps its existing protocol deadlines and finally cleanup rather than being
killed at the end of the checkout deadline. No default full fleet/provider
matrix is launched by this operation.

The preflight JSON names `candidate` (the exact gate/source SHA), a fresh
`observed_at_epoch` (at most 900 seconds old), and `participants` covering every
configured satellite. Each participant has `host`, `required` and `online`.
Only a configured `smoke_optional` host can be non-required. Offline optional
participants stay pending/PARTIAL; quota-only UNTESTED also stays PARTIAL
(exit 2). Neither establishes useful delivery. A required boot/drain timeout or
failed/malformed canary is an error after staging, with the pin left staged.
Inspect the pin and evidence before authorized recovery; do not blindly restage.

Each online/required participant's receipt records:

- `checkout`, `script`, `interpreter` and exact `python_version`; script and
  interpreter must be the dedicated checkout's satellite and `.venv/bin/python`.
- `installed_source_sha`: actual running satellite/emitter source commit bound
  to process/incarnation/import readback, rather than checkout HEAD.
- `previous_incarnation`: original boot identity captured before candidate boot.
- `installed_helpers_sha`, `installed_cli_version`, and `installed_cli_sha256`:
  actual helper source and a digest of the installed CLI release identity receipt.
- `dependency_digest`, `configuration_sha256`, `consumer_payload_sha256`: SHA256
  of retained interpreter/dependency inventory, canonical service/configuration
  identity manifest, and frozen authenticated installed-consumer replay evidence.
  Hash service/env-file bytes; keep secret values outside receipts and source.
- `imports_passed`, `compatibility_passed`, `recovery_ready`: true only after
  candidate import/dependency checks, installed-old/candidate-new daemon replay,
  and verified live-issued source checkpoints. Retain exact commands/results
  behind these claims. A version/configuration change invalidates affected proof.

The pin tool consumes this fixed deploy-window receipt; it does not provision,
SSH-update, restart or enable hosts. Keep the single exact global pin and daemon
equality policy. During transition, mismatched old satellites remain rejected;
retained original-source replay repairs that window after convergence.

## Rollback

In the same Nexus-authorized serialized window, restore the captured prior pin
with a readback before rolling coordinator back to its prior accepted SHA:

```
/opt/homebrew/opt/python@3.13/bin/python3.13 services/chat-stream-v2/tools/event_push_pin.py \
  --db /path/to/sessions.db --rollback \
  --gate-evidence /path/to/previous-public-release-merge-gate.json \
  --satellite-preflight /path/to/previous-satellite-preflight.json
```

Nonempty rollback waits for fresh restored incarnation, the affected Codex
canary, useful events and drained history under the same receipt contract.
If the captured previous pin was absent, rollback restores the existing no-pin
admission behavior. It does not direct satellites back to an earlier artifact;
restore their separately captured running artifacts and restart them in the
authorized window. Omit `--gate-evidence` only for an absent previous pin;
nonempty rollback targets require canonical evidence for the captured previous SHA.
Confirm `hello`, `list_sessions`, and `request_stream_events` still work and
record the return to the known central-capture freshness behavior.

### Failed-boot restoration

An unbootable process cannot receive a rollback pin. Before enabling updates,
rehearse the independent recovery path in a disposable checkout: force candidate
startup/import failure, restore the captured known-good checkout and its prepared
venv/service definition, restart through the supervisor, and run the real journey.
Retain old source objects, interpreter/dependency/service/configuration preimages,
previous global pin, transcript files and cursor/held-span metadata.

In an authorized recovery window, use the existing SSH route to restore only
that dedicated host component and restart its supervisor. For example, on a
Linux satellite use `git -C <dedicated-checkout> checkout --detach <previous-sha>`
and restore its captured venv/service preimages before
`systemctl --user restart pentacle-satellite`. On macOS restore the same preimages
and the captured launchd definition, then restart that job. Restore the global
pin once, coupled with the daemon only where the existing equality policy
requires it. Never rewind session/task databases or credentials. Confirm the
actual source/interpreter/boot incarnation, affected send/reply/history journey
and recovery drain before removing NO_AUTOUPDATE overrides. Source-only fixture
success does not certify a real host or enabled autoupdate.

## Diagnostic timestamps

The daemon and satellite entrypoints format their existing stderr logging as
`2026-09-14T19:00:00.123Z WARNING logger message`, using UTC regardless of the
host timezone. Severity, logger names and exception tracebacks are retained.
The daemon's `--log-level` and satellite's `PENTACLE_SATELLITE_LOG_LEVEL` continue
to control severity; an embedding caller's existing handlers remain authoritative.
No additional sink or message-content fields are introduced. After activation,
read each process's configured stderr sink and bind a fresh timestamped record
to its accepted artifact and PID. Historical records retain their original format.

## Usage-state collector (separate from the ingest satellite)

Each host also runs the usage-state collector so `agent-orch usage --host <h>` reads a
fresh, account-stamped quota. It is not part of the ingest satellite and has its own
timer/plist (`pentacle-usage-state-collector.{service,timer}`,
`com.pentacle.usage-state-collector.plist`, 600 s). Install, verify, enable and roll back
with `../install_usage_collector.py`; see `docs/usage_accounting.md` § Per-host collector.
