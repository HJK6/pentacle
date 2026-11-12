# Optional persistent assistant

The assistant is an ordinary top-level session identified by a configured role. It uses the existing chat view, transcripts, reports, questions, spawn reservations and handoff. This option adds no service, database or model router. It is disabled by default.

You give the assistant a name and an icon. This guide uses the default name **Zephyr**, shown with a machine sigil as its icon (see the screenshots in the mobile README). Both are yours to change: set the name when you bootstrap the assistant (the `--name` value below), and the icon follows the host's sigil/colour. The display name is data, never authorization. The public default is Zephyr. Set `PENTACLE_ASSISTANT_COMPOSITE_TITLE` privately to retain your own display name; clients prefer the daemon-provided name. Microphone recognition has its own single private override, `MIC_ASSISTANT_NAME` (see [microphone setup](../mic-server/README.md)); its default is also Zephyr. Existing composite addresses and stored identity keys remain protocol identifiers.

## Configuration

Set `PENTACLE_ASSISTANT_ROLE=assistant` in the daemon's private environment to protect that role on its local host. Set `features.assistantRole` to the same nonempty slug in each participating web/mobile private configuration. Leave these absent or empty for ordinary behavior. Public examples ship without an active assistant role. Restart the daemon after changing its environment and rebuild/reload clients according to their existing configuration workflow. Do not commit personal instructions, machine identities, credentials or context into these public repositories.

Clients pin an exact role match ahead of the normal attention order while retaining its attention indicators. They hide existing delete and rename controls and refuse local deletion and rename attempts. The mobile assistant row does not respond to sideways swipe gestures. Other sessions retain their existing behavior. A mismatched or disabled client may still show a delete action; the enabled daemon remains authoritative and returns `close_protected`. That error is terminal and must not enter a retry loop.

## Bootstrap your own assistant

Run this from a public checkout with its Python dependencies and candidate `agent-orch` installed, tmux and a working provider CLI. In every shell used below, activate the installed environment (`source .venv-dev/bin/activate` after [developer installation](developer_onboarding.md#1-install-prerequisites)), or use that environment's Python executable explicitly. The provider account must already be logged in and support your explicit model/effort; check the installed `agent-orch models` catalog as well as your account. The assistant's name (for example Zephyr), physical host label (`local` here), provider/model and protected task role (`assistant`) are distinct. Keep instructions, process memory, credentials, config and stores outside the checkout. [Agent setup](../AGENT_SETUP.md) covers ordinary installation; this wrapper adds the named protected backend and exact composite configuration.

1. Prepare a private working directory and a compact instructions file. The example paths below are operator-chosen variables, not host defaults. Do not overwrite existing files. Include references to your private `soul.md`, process `MEMORY.md`, active work and commitments; the soul owns local paths/capabilities, while memory owns facts/preferences/decisions. Copy [the process kit](../process/README.md) separately when desired, and configure the daemon's `PENTACLE_MEMORY_ROOT` and CLI's `AGENT_ORCH_MEMORY_REPO` to that copy. Role discovery uses `agents/assistant_baseline.md` in the CLI memory root when you author one privately; the bootstrap passes the explicit instructions file to initial spawn without modifying it.

```sh
PRIVATE_WORKSPACE="$HOME/pentacle-private/assistant"
PRIVATE_STATE="$HOME/pentacle-private/state"
INSTRUCTIONS_FILE="$HOME/pentacle-private/instructions.md"
OPERATOR_CREDENTIAL="$HOME/pentacle-private/operator.token"
mkdir -p "$PRIVATE_WORKSPACE" "$PRIVATE_STATE"
chmod 700 "$PRIVATE_WORKSPACE" "$PRIVATE_STATE"
# Create your instructions privately, then set these two roots if using the kit:
# export PENTACLE_MEMORY_ROOT="/absolute/path/to/private-process"
# export AGENT_ORCH_MEMORY_REPO="$PENTACLE_MEMORY_ROOT"
```

2. Issue a `pentacle` operator credential using the daemon owner's registry. This registry must be the same one read by the daemon (the default registry is home-local). The command prints secret material, so redirect it into a private file and extract only `code`; never paste/log its output. Run once for a fresh credential destination, preserving any existing enrollment. Use [operator authentication](REMOTE_AUTH.md) for rotation and mobile enrollment.

```sh
umask 077
test ! -e "$OPERATOR_CREDENTIAL" && test ! -L "$OPERATOR_CREDENTIAL" || exit 1
python3 services/chat-stream-v2/tools/operator_auth_cli.py issue \
  --client-kind pentacle --label 'Own assistant Web' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])' \
  > "$OPERATOR_CREDENTIAL"
chmod 600 "$OPERATOR_CREDENTIAL"
```

3. Start one owner-controlled daemon with role protection on and composite off. Use the correct provider executable, transcript root and private machines file from [orchestration setup](agent_orchestration_setup.md). Keep the machines-file host name equal to `--local-host`; `local` is only a synthetic single-host label. Record the PID, start/stop command and all store/registry paths. A foreground terminal is sufficient; no service installation is needed. This initial phase must not point at a production or existing assistant installation.

```sh
export PENTACLE_ASSISTANT_ROLE=assistant
export PENTACLE_ASSISTANT_COMPOSITE_ENABLED=0
# Leave PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS unset (default []).
python3 services/chat-stream-v2/main.py \
  --host 127.0.0.1 --port 7791 --local-host local \
  --db "$PRIVATE_STATE/sessions.db" \
  --notifications-db "$PRIVATE_STATE/notifications.db" \
  --assets-db "$PRIVATE_STATE/assets.db" --blob-root "$PRIVATE_STATE/blobs" \
  --spawn-cwd "$PRIVATE_WORKSPACE"
```

4. In another shell, invoke the actual [bootstrap tool](../tools/bootstrap_assistant.py). All authority-bearing inputs are explicit; it never selects an inherited endpoint or invents an operator CLI flag. It uses the daemon's operator nonce challenge/proof, requests a top-level protected role, and inspects the ready backend's actual stream, generation and effective tuple. Optional `--account` selects an alias and verifies its actual binding, retained in the intent and receipt. It refuses existing outputs/bindings rather than replace an installation. `--dry-run` validates inputs and prints a plan with no network, spawn or file mutation; remove that flag to provision. The tool preserves the instructions and writes `assistant.env`, `assistant-config.json`, `assistant-receipt.json`, `assistant-client.cjs` and a durable `assistant-bootstrap-intent.json` with mode 0600. The intent records the exact request/backend before spawn so an ambiguous failure cannot create a second owner. Partial failure is an inspect/recover action, not permission to blindly spawn again.

```sh
python3 tools/bootstrap_assistant.py \
  --url ws://127.0.0.1:7791 --credential-file "$OPERATOR_CREDENTIAL" \
  --physical-host local --name Zephyr --provider codex \
  --model gpt-6-sol --effort medium \
  --private-workspace "$PRIVATE_WORKSPACE" \
  --instructions-file "$INSTRUCTIONS_FILE" --dry-run
```

The machine-readable receipt separates `composite_stream_id` (`local:assistant`) from the returned `backend_stream_id` (`local:assistant-backend-…`), `backend_generation`, `effective_tuple` and `bootstrap_state`. A provider-native ID is not a session generation. The display name is quoted as data; do not construct shell commands from it or use it as authorization.

5. Stop only the recorded daemon PID, retaining the live backend tmux pane. Load the emitted `assistant.env` in the daemon owner's launch environment and restart the exact same daemon command with the **same stores, registry, machines file and backend**. For a managed installation, use its existing owner-authorized restart procedure and feed those variables into that service explicitly; sourcing an interactive shell does not update a service. The wrapper never restarts anything. It leaves exception recovery off; no private work-item name or title grants rebind power.

```sh
# After stopping only your recorded daemon:
. "$PRIVATE_WORKSPACE/assistant.env"
# Repeat the exact daemon command above with unchanged stores/registry.
# In a separate shell, repeat the bootstrap invocation above using
# --readback in place of --dry-run. It is read-only and checks exact binding,
# backend readiness and configured composite title against the receipt.
```

Readback must show `activation: restart_binding_verified`, the exact returned stream/generation, binding `source` (`env` initially) and `revision` (0 initially). A stale pair fails closed; do not invent a new generation or edit SQLite. The immutable startup setting `PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS` is a JSON array, default `[]`; malformed values fail startup. An explicit opt-in requires a matching qualified spec with verified grant provenance on a live parentless seat, subject to the recovery authority described below. Naming a title/role/spec without that grant gives no privilege. The happy-path recipe leaves this setting unset.

6. Build/serve Pentacle Web using the generated `assistant-client.cjs` as the private config (`PENTACLE_CONFIG` or `--profile`), following [the web host guide](../server/README.md). It carries the same physical host, credential-file path, `features.assistantRole: assistant`, mic off and experimental Chat on so the composite can be selected. Select Zephyr's `local:assistant` row, send one typed input and see the correlated provider answer in the browser. Queue persistence or `submission_confirmed=false` alone is not a reply. Retain the input/dispatch IDs, backend generation, canonical publication receipt/event and actual effective tuple privately. Have the backend follow the daemon-authored `assistant publish` contract exactly, preserving answer text and IDs on retry.

For Web/mobile away from the daemon machine, loopback is not reachable. Use the physical host's permitted VPN/LAN address in the client endpoint and the documented authenticated listener/access setup; a phone's `localhost` is the phone. The generated client URL is the bootstrap URL; change your private client endpoint deliberately for remote access. Mobile needs its own enrolled credential and matching `assistantRole`, following [mobile setup](https://github.com/HJK6/pentacle-mobile/blob/main/AGENT_SETUP.md). Composite custom title comes from the daemon; no legacy assistant alias or client rebuild is needed to choose the typed name. Provider login, supported account tuple, physical phone/signing and microphone provisioning remain separate installation prerequisites.

The microphone source is available at [mic-server](../mic-server/README.md), but capture and `features.mic` stay off by default. The listener currently uses a fixed built-in assistant wake; changing the typed display name does not change it. Custom natural wake and mobile legacy alias migration are deferred. Do not enable voice merely to validate typed bootstrap.

## Binding readback and recovery

From the current backend's actual issued seat-token environment, `agent-orch assistant binding` reads the effective `stream_id`, `generation`, `source` and `revision`. The owner may hot-rebind with the supported candidate CLI:

```sh
agent-orch assistant rebind --target "$SUCCESSOR_STREAM" \
  --generation "$SUCCESSOR_GENERATION" --expected-revision "$BINDING_REVISION" \
  --request-id "$STABLE_REQUEST_ID"
agent-orch assistant binding
```

Through `assistant rebind`, only a current binding owner, an authenticated handoff successor, or the narrowly configured exception can mutate it. The daemon-owned restore described under [Automatic restore of the bound seat](#automatic-restore-of-the-bound-seat) is a separate operation with its own checks. Revision CAS, exact live generation/tuple and replay fences remain authoritative. `--clear` removes the durable override only when the startup env binding is still usable; it is not a way to repair a stale env pair. Use owner-authorized `agent-orch spawn --handoff` for continuation and inspect its returned successor/retirement evidence, then read back binding. A dead/stale owner requires the existing authenticated operator recovery/handoff journey; do not run the fresh bootstrap again over retained outputs. Inspect request receipts before retries. The bound-seat restore operation refuses role-protected holders (`PENTACLE_ASSISTANT_ROLE` equal to the holder's role); use their documented recovery path. A bound seat that is not role-protected can be restored by the daemon, as described in [Automatic restore of the bound seat](#automatic-restore-of-the-bound-seat).

### Rebinding after a seat resume

A host logout kills the bound seat's pane. Normally the daemon resumes and rebinds an unprotected bound seat automatically ([Automatic restore of the bound seat](#automatic-restore-of-the-bound-seat)). Use the manual contingency below only after determining that an automatic episode is no longer in flight. A manual `agent-orch spawn --resume <session-id>` reopens the same stream at a new generation while the binding still names the old generation. The configured exception lets a qualified resumed front desk rebind itself:

1. Set `PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS` to a JSON array naming one dedicated recovery work item that is attached only to front-desk seats.
2. A fresh parentless recovery commission requires a configured, qualified recovery work item with matching `spawn_explicit` or `drive_explicit` provenance and a nonblank granting principal. A different-seat handoff requires the existing generation-bound succession proof; inherited membership alone never authorizes rebind. Inherited `handoff_inherited` provenance supports only same-seat recovery where the actor and target both equal the old named binding stream and `clear=false`; live-generation, revision/CAS and visibility checks still apply. The primary exception permits `default` or `hidden`; Daff's hidden exception is unchanged. The daemon hides the actual local bound FD, its bound handoff successor, and a deliberate fresh recovery commission before first open. An ordinary seat carrying inherited recovery tags retains its own visibility. Schedule owner-spec fallback is inherited attribution: fresh scheduled recovery needs the original explicit selection proved by its authenticated admission receipt, including recurring templates and updates. Missing or expired proof grants no recovery exception while ordinary scheduled work continues. Read back topology, provenance, tuple and binding after succession.
3. After `spawn --resume`, before anything else, the resumed seat runs `agent-orch assistant binding`, then `agent-orch assistant rebind --target <own stream> --generation <own new generation> --expected-revision <revision just read> --request-id <one id per generation>`, then reads the binding back and checks stream, generation and revision. Re-read the revision immediately before the rebind. After a lost reply, repeat the same request id: it replays the receipt with `duplicate: true`. `assistant_rebind_stale_revision` means read again. A refused request id stays refused, so use a new id once the cause is fixed.

While the bound seat is dead, tells queue and are flushed after the rebind; a tell held by the front-desk digest counts as delivered. An input to a dead local bound seat is queued (`assistant_awaiting_recovery`) as soon as the daemon proves locally that the pane and process are gone, including in the window after a daemon restart before the reconciler records the death. Queued inputs replay only to the same generation or to an exact verified successor: a daemon restore episode, a handoff proof, or a committed host-transfer successor. A plain `spawn --resume` followed by a manual self-rebind is not verified lineage, so inputs queued for the old generation end `routing_failed` (`assistant_recovery_successor_unverified`) instead of replaying. Inspect the route state before resending any input shown failed, so nothing is dispatched twice. Limitation: a holder on another host gives no local death proof (`remote_holder`), so an input to a dead remote holder is not queued this way; check its route state after recovery.

This recipe is for an unprotected front-desk row. When `PENTACLE_ASSISTANT_ROLE` equals the seat's role, a dead row is preserved and `spawn --resume` is refused `resume_session_already_live`; use the authenticated handoff recovery above. A rebind never restores the lifecycle-manager grant. The journey is exercised on a disposable daemon by `PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_fd_resume_recovery.py` (in `services/chat-stream-v2`).

### Automatic restore of the bound seat

The daemon can bring a dead bound seat back itself: it resumes the same Claude session on the same stream and moves the binding to the resumed generation. It is off by default.

**What it covers.** A direct-primary composite whose bound seat runs on the daemon's own host, is a Claude seat with a recorded session id, and is not role-protected. A role-protected holder, a router-backed composite, a seat on another host and a non-Claude seat are refused and reported as `ineligible`. It never designates lifecycle authority. A lifecycle-manager grant the operator already gave to the bound seat moves with the seat only in the case described under "The lifecycle-manager grant" below.

**Turning it on.** Set `PENTACLE_ASSISTANT_AUTO_RESTORE=1` in the daemon's environment and restart the daemon. Unset, empty or `0` leaves it off; any other value stops the daemon at startup. To hold it during planned maintenance, create the file `assistant-auto-restore.inhibit` beside the daemon database; remove the file to resume. The file stops new automatic work only: a resume that was already sent is still followed to its end, and an operator request is not held.

**When it acts.** Only on proof that the bound seat is dead, checked when a restore starts and again before every attempt:

- the bound row is still the generation the binding names, and is either open or was closed by the reconciler as a dead pane (a seat closed by an operator, by itself or by a handoff is left alone);
- a fresh `tmux has-session` for the seat answers "no such session" (a timeout or transport error is not proof);
- the pane process recorded for the seat is gone: `ps` reports no such process, or the pid now belongs to a process with a different start time.

A daemon restart with the pane alive does nothing. If any check cannot be made the daemon waits and reports `waiting_evidence`; it does not guess.

**What it does.** One restore is recorded per dead generation and survives daemon restarts. Each attempt resumes the seat with `--resume` under the daemon's own identity, keeping its stream id, provider session, role, visibility and work items, and waits for the seat to be ready. An open preserved parent is kept. A missing or closed preserved parent is cleared only for this internal restore resume, under graph admission, with a durable `parent_closed_resume_top_level` receipt in the spawn intent and restore audit. This records admission, not successful restoration. Ordinary manual resumes, including `--top-level`, and new children still refuse unavailable parents. No parent row or children are rewritten.

The binding then moves by compare-and-set: if it is still the pair and revision the restore started from, it becomes the resumed generation at revision + 1, with a row in the rebind audit under the actor `daemon:assistant-restore`. Automatic restore uses this existing transaction, without a manual self-rebind. If someone rebound the assistant in the meantime, the restore does not change the newer binding and ends as `superseded`; any seat it already resumed stays open and unbound, like the previous holder after any rebind. A rebind made after a successful restore bind is an ordinary hot rebind and remains authoritative: the restore's notice and its revision describe what the restore did, not who holds the assistant now. After a successful bind, queued tells and verified-lineage inputs are delivered and the seat is sent one notice that it was restored, as an ordinary tell (the front-desk digest may hold it like any other). The notice states that front-desk authority follows the binding.

**Front-desk authority.** Restore copies no grant. FD authority is derived from the binding, so the resumed generation has it once the bind commits, unless the operator revoked it; the bind transaction writes one `fd_binding_change` audit row. A restore superseded by a newer binding leaves authority with that binding.

**Transcript history after a resume.** A resumed seat is reopened as a new lifecycle and the daemon reads its transcript again from the start. Records an earlier lifecycle of the same stream already recorded are kept as this lifecycle's history, but they are not new events: they are not sent to clients as live events and they are not mirrored into the assistant chat a second time. This applies to every resume, not only the automatic restore.

There are at most four attempts, 30 seconds, 2 minutes and 10 minutes apart. After the fourth failure the restore is `degraded` and stays that way until an operator retries it. A daemon restart does not refill the attempts. An attempt whose outcome is still unknown after 10 minutes also ends the restore as `degraded`; a second attempt is never started on top of an unresolved one. This deadline also covers failed adoption before generation admission when a surviving pane prevents another death proof: it uses the original attempt start, leaves that pane untouched and starts no overlapping attempt. A provider or authentication failure may show up as a launch that fails outright or as an attempt that never resolves; either way the bounded path above ends the restore as `degraded`. The journeys below prove the handling of a failed launch and of a stalled start with a stand-in provider, not real provider authentication.

Degradation also creates one message-only **&lt;configured assistant name&gt; recovery stopped** warning in the existing Updates feed, including when the backend is absent. It states the stop time, attempt count and reason. A durable `degraded_notice_published` restore-audit receipt tracks each assistant/episode/budget epoch. After interruption, publication reconciles notification rows across open, resolved and expired states before creating anything. A dismissed or expired historical warning is not refreshed or recreated for that epoch; a new operator retry budget has a new key. Notification failures retry publication on later ticks without spawning or consuming restore budget. Startup also checks an already-degraded episode, including with automatic recovery disabled. The historical warning remains after recovery; binding status supplies the current state. This adds no action button, agent retry privilege or CLI command.

**Reading the state.** `agent-orch assistant binding` (the `assistant.binding` request) returns a `restore` object:

| Field | Meaning |
|---|---|
| `state` | `disabled`, `suspended`, `healthy`, `ineligible`, `waiting_evidence`, `pending`, `spawning`, `spawned`, `bound`, `restored`, `superseded` or `degraded` |
| `reason` | why, when there is one: for example `holder_revived`, `pane_probe_unknown`, `process_probe_unknown`, `binding_moved`, `attempt_unresolved`, or the spawn error of the last failed attempt |
| `episode_id`, `trigger` | the restore record and whether it was started automatically (`auto`) or by an operator (`manual`) |
| `attempt_seq`, `budget_used`, `max_attempts` | attempts ever started, attempts used in the current round, and the limit of 4 |
| `next_attempt_at` | when the next attempt may start |
| `predecessor_generation` | the dead generation being restored |
| `updated_at` | when the record last changed |

Restore copies no grant; a primary bind records `grant_carry=binding_derived` on the episode row and one `fd_binding_change` lifecycle audit row.

`restored` is shown while the binding is the one that restore produced. Connected clients also receive an `assistant.restore.changed` event on each change, and the daemon logs one warning line. No shipped client draws this state yet; read it through the request above.

**Operator request.** An authenticated operator can send `assistant.restore` with a `request_id` and `action`:

- `restore` starts a restore now if the seat is proven dead, also with the automatic option off. If one is already running it returns its state.
- `retry` gives a `degraded` restore four new attempts. Earlier attempts stay in the audit.

Refusals: `assistant_restore_unauthorized` without operator authority; `bad_request` for a missing `request_id`, one longer than 200 characters, or another `action`; `assistant_restore_not_degraded` when `retry` finds no `degraded` restore for the current binding; `assistant_restore_attempt_unresolved` when the restore degraded with `attempt_unresolved` and that attempt's resume request has not yet ended as failed.

Repeating a `request_id` returns `duplicate: true` and does nothing. There is no `agent-orch` subcommand or client button for this request yet.

**Records.** `v2_assistant_restore_episode` holds one row per restore (including the grant it recorded and what the bind did with it) and `v2_assistant_restore_audit` one row per step (created, each attempt and its outcome, bound, routing completed, exhausted, superseded, budget reset, operator request). The journeys are exercised on a disposable daemon by `PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_assistant_auto_restore.py` (in `services/chat-stream-v2`), with a stand-in provider. Before enabling this on an installation, the front-desk enablement gate requires one real-provider restore on a disposable daemon; the journeys above use a stand-in provider and do not establish that proof. The option stays off by default, no client button exists, and the state is readable through the API only.

### Automatic restore of the ruling authority

The direct primary records the live authority and its instructions together:

```sh
agent-orch assistant authority --set <stream-id> --brief-file <absolute-path>
agent-orch assistant authority
```

The recorded shape includes the seat's provider, model, effort, role, visibility,
spec, generation and brief hash. The observed account is audit information; a
replacement uses the current host/provider default. Keep the brief available on
the daemon host. `--set` requires the brief unless disabling the authority.

After a recorded authority is continuously lost for 60 seconds, the daemon
spawns a replacement on the local host. It binds only after the attempt's own
delivered outcome, matching generation, open session and live pane are proven;
the session must be `ready`, or `started` with that delivered outcome. Pending
covered work is retargeted. Three attempts per budget epoch use 60- and
180-second backoffs; no more than six replacement spawns start in 24 hours.
The daemon reconciles an interrupted attempt before making another: the
dispositions are `adopted`, `attempt_failed`, `never_admitted`, and
`still_unresolved`. An unresolved attempt never causes a second spawn while its
outcome remains uncertain.

`agent-orch assistant authority` and `agent-orch inspect <front-desk>` show
restore state: `healthy`, `restoring`, `unresolved`, `exhausted`, `ineligible`,
`inhibited`, or `disabled`. Notices include `authority_unavailable`,
`authority_restore_ineligible`, `authority_restore_inhibited`,
`authority_restore_unresolved`, `authority_restore_exhausted`,
`authority_restore_rate_limited`, and `authority_restored`.

The direct primary can run `agent-orch assistant authority --restore` to skip
the debounce, or `--restore --brief-file <path>` to record or replace a stale
shape from the present dead row. `--retry` reconciles the prior attempt and
starts a fresh budget only when it has failed; an uncertain attempt is refused
with `attempt_unresolved`. A manual `--set` supersedes recovery. `--disable` is
an audited last resort that releases pending covered work unruled.

Automatic recovery is enabled by default; `PENTACLE_AUTHORITY_RESTORE=0`
disables new automatic episodes. The file
`authority-restore.inhibit` in the daemon state directory inhibits automatic
recovery; manual recovery still runs. If the dead authority row is absent and
no usable shape exists, keep the marker in place while an already-authorized
non-front-desk owner spawns a successor. The front desk then sets the live
successor with its brief, reads back `state ready`, and removes the marker.
Authority recovery waits for a bound front desk. Automatic recovery of an
absent front desk depends on the bound-seat restore being configured and
eligible; otherwise restore the front desk manually first.

For reproducible installation evidence, run [the portable first-turn gate](developer_onboarding.md#7-run-validation). It exercises the actual bootstrap, daemon, tmux, native-format counterpart's own candidate publish CLI and real browser, including duplicate publish, stale-generation refusal and restart readback. It does not establish paid-provider login, mobile hardware, microphone or private runtime activation.

## Activation and context

Use the existing authenticated operator or trusted service spawn interface to create a top-level session on the daemon host with the configured role, chosen provider/model/effort, and private initial instructions. An ordinary agent token cannot grant or remove the protected role. Operator/service role changes use the existing role-set interface. The role does not confer general orchestration authority.

Private deployments may place an `agents/<role>_baseline.md` in their configured memory root so the existing orchestration CLI prepends it on spawn. The kit ships a starting point for the assistant itself in [`process/agents/assistant_baseline.md`](../process/agents/assistant_baseline.md): what wakes the front desk, context limits, outcome-first decisions, and advisor and external-agent etiquette. Copy it into your private root and adapt it. The daemon does not invent or manage personal memory. The bootstrap should load a compact private context file and link existing work/receipt records. Record new commitments before acknowledging them, and retain dispatch IDs so a successor checks delivery evidence before retrying. A conversation checkpoint is not evidence that a project completed.

## Continuation and failure

Exactly one assistant holder is admitted, except for the bounded overlap during an existing managed handoff. Concurrent activation and role grants are serialized; unresolved persisted spawn intents also prevent a duplicate activation. Repeating the same spawn request keeps existing idempotent replay semantics.

The current assistant may use its verified token for `spawn --handoff`, preserving the role and staying on the daemon host. Authenticated operator recovery uses the existing spawn handoff fields with the prior stream as `handoff_from_stream_id`. Successful launch uses the existing managed predecessor close. A successor should wait until it is the sole open holder before writing shared context or dispatching work. If cleanup is uncertain, inspect the existing receipt and predecessor instead of starting another replacement.

Ordinary operator, self, idle-reap and sweep closes are protected. Only internal `handed_off` and `spawn_rollback` close kinds bypass protection; a client-supplied `close_kind` cannot authorize deletion. Confirmed pane death leaves the protected row open with death evidence so its existing client entry remains available for recovery. Recovery is explicit; there is no new automatic restart loop. A daemon restart reconstructs the same session and pending-intent state.

Existing owner-authorized scheduled handoffs can provide a future continuation. Assistant authority is checked at schedule admission; internal dispatch uses the scheduler's trusted identity. At fire, a protected handoff requires the owner generation recorded at admission; an obsolete or unbound schedule fails as `stale_owner_generation` before launching a successor. The daemon holds that generation's lifecycle fence through boot and retirement. Track the accepted schedule receipt and keep obsolete wakeups canceled. A locally hosted model alone does not provide offline phone access.

## Fleet lifecycle authority

By default no session may close or reparent a session it does not own: close stays limited to self, direct parent and the authenticated operator; reparent to the worker's current parent or a live handoff successor of that parent. Naming yourself the new parent confers nothing, so a top-level stream, including a protected assistant, has no ordinary reparent owner.

**Front-desk mode (a configured primary direct binding).** The lifecycle principal is the primary assistant's current effective binding: its stream and exact generation, read inside the Store transaction that admits the action, at an open generation that a host transfer has not retired or only prepared. Rebind, managed handoff, restore, daemon recovery and host-transfer commit therefore move FD authority with the binding, with no consent, designation or grant copy. An unavailable, retired, stale-generation or unreadable binding grants nothing; there is no fallback to a previous seat, the legacy holder or another assistant. No role, title, old binding, lineage or configuration entry confers it. Each committed binding transition writes one `fd_binding_change` audit row (old/new pair, binding revision, actor, reason, request or episode id) in its own transaction; a replay writes none.

The legacy singleton grant row is only a policy in this mode: no row or a populated holder is `enabled`, a nonzero revision with a null holder is `revoked`. A populated legacy holder, open or closed, keeps no privilege. The first start in this mode writes one `fd_policy_activation` audit row; later starts write none. Authenticated operator revoke (`consent request revoke`, or `lifecycle revoke --emergency` on loopback) disables FD lifecycle authority across every later swap until re-enabled. Designation in this mode only re-enables the current FD: a target other than the bound seat is refused (`authority_target_not_front_desk`), as is a stale target generation or expected revision. The re-enable writes the then-current pair as a rollback snapshot that never authorizes. Closing an obsolete holder has no authority effect; closing the current FD removes the capability until an already-authorized binding transition or restore succeeds. Nothing elects a replacement.

`agent-orch lifecycle inspect` returns `effective`: `source` (`binding` or `legacy_grant`), `holder_stream_id`/`holder_generation`, `binding_revision`, `binding_available`, `policy_state` and `policy_revision`, beside the raw `grant` row and audit.

**Without a direct binding** the grant names the one holder generation as before. The operator designates or revokes it through phone consent:

- **Designate / replace / revoke:** `assistant.lifecycle` creates a phone approval challenge and returns `consent_pending`; it never directly changes the grant. The web menu requests approval on the phone. `agent-orch consent request designate --target <stream> --reason <why>` or `consent request revoke --reason <why>` requests the same journey. A verified seat may request, but only a current operator-authenticated mobile credential owning an active audience key may approve. Each challenge binds the action, exact target generation, expected revision, requester, audience, nonce and expiry. The app signs the daemon bytes with its enrolled approval key; one Store transaction verifies the signature, mutates, consumes and audits. A chat answer or ordinary notification answer is never approval.
- **Transfer:** disabled (`authority_transfer_disabled`). `agent-orch consent status|cancel <challenge-id>` reads or cancels a challenge; deny is admitted only from a current audience mobile device. Approval/deny do not participate in reconnect replay. Exact signed-tuple approval retry returns the stored receipt; a different tuple conflicts.
- **Eligible recipient:** an open session with a positively observed live pane and `ready` bootstrap (unknown state fails closed), not offline or presumed dead, whose daemon-recorded role is `lead`, or the protected assistant role.
- **Handoff:** a successful protected-assistant handoff moves the grant to the successor only when the source is the current holder and the successor is eligible. Front-desk mode has nothing to carry.

**Manager close (current FD or holder).** One close path serves both modes, for any ownership shape: its own child, a non-child, another parent's (hidden) child or a top-level handoff successor. It requires an explicit `--reason` (CLI defaults are refused) and request id; no live children or pending child spawns; a target that is not a protected assistant and not the lead or visible chat of an open operator-owned lane (`close_operator_lane_protected`; that lane keeps its own confirmation); and fresh idle proof for a live pane, while an offline target records a generation-fenced deferred retirement, never a claimed death. A missing report or outstanding disposition is returned as a warning for accountable owners. An explicit FD `--force` request with a reason and request id may recover its own freshly proven busy child with no live children or pending spawns; disposition debt stays owed in `work owed` with FD custody. Idle or unknown capture does not grant busy override; the spec is never marked complete by close. External lane-ruling requirements still apply. An `admitted` row precedes any kill and an audit failure refuses it. Repeating a completed close returns `close.already_closed`. Audit rows use `actor_kind=front_desk` in front-desk mode and `manager` otherwise. An independently selected advisor is an ordinary close target.

**Manager reparent.** Allowed for the principal with an explicit reason, request id and expected generation; protected assistants and cycles are refused.

**Linearization.** Consent approval, revoke and every manager close/reparent hold the daemon authority lock from admission through effect and outcome audit. Binding commits (rebind, restore, recovery, host transfer) and every close/reparent effect share the daemon graph lock; the principal is re-read under it immediately before the kill or the reparent write, so a swap or revoke lands before or after an admitted action, never during it. Lock order is: composite binding lock, assistant policy lock, authority lock, the target's lifecycle lock, then the graph lock; nothing holding an inner lock acquires an outer one. Once a manager action holds its locks, admission, effect and outcome audit run to completion even if the request is cancelled. Cancellation while still waiting for the locks leaves no effect.

Audit rows record the verified actor (`operator:<credential id>` with no generation, or the seat's stream and generation) and the policy revision in force; refused attempts by verified seats are audited too. Caller-supplied actor fields are ignored. Reason and request id are bounded, control characters stripped and credential-shaped runs redacted; an unauthenticated caller's free text is not stored.

**Rollback to a pre-binding runtime.** Before admitting older code, the coordinated release runs the rollback projection (`lifecycle_authority_project_for_rollback`) once: it writes the then-current eligible FD as the legacy holder, or no holder when revoked or unavailable, with an `fd_rollback_projection` audit row. It is never run per swap. Rolling forward again keeps an enabled policy enabled and a revoked one revoked. After rollback the pre-binding semantics apply: that runtime admits the bound FD's close from the binding alone and never reads the grant, so the bound FD can close and revocation is enforced only by this runtime. The projection governs the grant-backed manager path, and a stale former FD seat gains nothing.

### Logical front-desk delivery

In front-desk mode `assistant`, the primary composite, the bound seat and any seat id the daemon's own records prove was primary-bound (env pin, binding row, and binding-change or legacy grant-move audit, less any seat bound to another assistant name; rebind audit rows name no assistant and are never evidence) are one logical FD tell address. A tell to any of them is routed to the current binding, or durably queued while unbound, even if the old seat is still open. Former FD ids are tell aliases only; they confer no authority, and physical `send`, inspect and audit still name the real seat. FD tell idempotency binds the sender, logical destination and body: a retry after a rebind returns the original committed outcome without a second paste, and a changed body or sender under the same key is `tell_id_conflict`. A receipt written before this change matches only when its seat is a proved FD seat, its retained sender is the same sender and its body is the same. Receipts keep `requested_to_stream_id`, the actual `to_stream_id` and `binding_revision`.

Held front-desk rows for a proved former FD seat fold into the current binding's digest with their original deadline and members. A claimed front-desk digest, child report, ruling result or authority loss/restoration notice addressed to a former FD seat, or to the current stream at an older generation, moves to the current FD pair only while no attempt evidence exists for its tell id; an uncertain attempt stays fenced to its recorded seat. While no FD is eligible such a notice is deferred without an attempt (`awaiting_front_desk`) and delivered once a binding is eligible; it never pastes into an obsolete seat. Error alerts keep their superseded-binding rebind. Operator-question answers and ordinary worker notices keep their producer and generation binding.

`send assistant` samples the binding once and re-checks it under the front-desk effect gate, held through physical input; a concurrent rebind commits before the check or after the paste. If it changed, nothing is sent and the result is `not_landed` with reason `assistant_binding_stale`; retry with a new request id. A committed send keeps its physical receipt.

An advisor selection equal to a proved FD seat is the logical primary (`same_primary`) and follows the binding; an independently selected advisor keeps its own generation checks and restore budget. An unexecuted ruling requested by a former FD principal, including the same stream at an older generation, ends as `release_blocked` with `requester_superseded` and its one result notice reaches the current FD; it is never executed or reissued automatically. The supersession check and the release effect hold the front-desk effect gate, which every primary binding change (rebind, restore, host transfer) also takes, so no swap commits between them. The current FD closing its own child takes the same FD close path as any other target: explicit reason and request id, ordinary idle and disposition checks, operator-lane fence and report waiver audit. Only explicit recovery of its own proven busy child bypasses idle and disposition checks, retaining debt with FD custody; binding, generation and parentage are checked at final admission. Lock order is the FD effect gate, then a seat lifecycle lock; a question release takes the gate before its producer's lifecycle lock, and an immediate bypass inside the requester's own `prompt ask` takes no gate.

## Validation

`services/chat-stream-v2/tests/test_lifecycle_authority.py` covers lifecycle mutation fences, disabled transfer, revocation, manager close/reparent fences and retired handoff replay. `test_fd_close_visible.py` covers binding-derived authority, swaps, policy activation, revoke persistence, rollback projection and races; `test_assistant_composite_addressing.py` and `test_front_desk_digest.py` cover logical FD tells, notices and the send fence; `test_fd_authority_qa_boundaries.py` covers own-child FD closes, stale advisor/ruling effects, alias provenance, notice deferral and gate serialization. `services/chat-stream-v2/tests/test_assistant_role.py` covers default-off behavior, ordinary close protection, role authority, concurrent activation, idempotent replay, pending intents, managed dead-session handoff, retained recovery entry, scheduled admission and forged managed-close fields. `services/chat-stream-v2/tests/test_consent.py` covers enrollment, signed challenges, refusal, rollback and revocation linearization. Existing role, handoff and scheduler suites cover their surrounding contracts. See [phone approval enrollment and recovery](REMOTE_AUTH.md#phone-approval-keys). `services/chat-stream-v2/tests/test_consent_host_ceremony.py` rehearses the actual candidate CLI and WebSocket handlers with synthetic signatures and an isolated home, registry and local-admin token. Run the daemon merge gate and each client's documented gate before activation. A live provider/installed-client readback remains distinct from deterministic fixture evidence.

## Composite authority and routing

For the optional `assistant_composite_v1` chat, the local classifier routes work
admission, prioritization and cross-project coordination as `new_topic` to the
configured authority backend. Delegated uncertainty about priorities does not
require an intermediate conversation turn. Unresolved consent still requires
clarification; routing itself never grants execution permission.

The current configured authority may admit and publish against a resolved,
authenticated operator input even when that input was initially routed to the
conversation backend. This standing authority does not extend lead-only
question or terminal-report permissions. Original input/dispatch correlation,
current session generation, lane scope, expected versions and publication
evidence remain required. Decision/terminal wakes use the authority admission
receipt to recover context, independently of the original router destination.


The configured conversation backend can request authority coordination with
`agent-orch assistant operation --operation authority.request --request-id <stable-key> --composite-stream-id <composite> --dispatch-id <original-dispatch> --payload '{"reason":"Coordination needed"}'`.
This operation accepts only a bounded reason on a resolved authenticated operator
dispatch owned by the current conversation backend. It atomically records one
existing-outbox notice per original dispatch, preserving literal input, IDs and
bounded routing context. It does not change the route owner or grant authority.
A different request key for the same dispatch conflicts; an identical replay
returns the current original notice receipt. Queued is not delivered. A changed
or unavailable configured authority generation leaves an inspectable failed
notice instead of silently redirecting to a replacement.

Routine hidden-backend ingress reports `persisted` and
`submission_confirmed=false`; it is not provider delivery. A suppressed queued
notice ends as persisted-only rather than retrying a wake. Composite lane
inventory is not the global work inventory: an empty list does not prove no
work, owner or progress. Status summaries need correlated current evidence;
when it is insufficient, the conversation backend requests authority review
and the authority publishes its own response.

Agent peers may address the primary assistant composite by stable literal
`assistant` or its configured name (`PENTACLE_ASSISTANT_PRIMARY_NAME`, default
`assistant`), with
`agent-orch tell` or `agent-orch send`. Each attempt resolves the current
binding once. A tell becomes the ordinary composite tell, and a send becomes
one ordinary physical send to the bound seat. The alias carries no operator
authority, adds no ledger, receipt or hold migration, and keeps per-transport
retry limits, so a resubmitted send after a move can deliver twice. See
`services/agent-orch/README.md` (Addressing the assistant by alias).

### Question-card rulings

A `prompt.ask` from the authenticated bound direct primary is held after
validation and producer admission, before a question is created. The advisor
receives the frozen card and may approve, revise, or deny it. Approval publishes
the same card; a revision or denial posts nothing and returns the reason. Use a
fresh question ID for a changed card. Other visible producers and the unused
composite `question.open` proxy are outside this gate.

A held ask returns `prompt.ask.held` (exit 75); a terminal non-posting ruling
returns `prompt.ask.ruled` (exit 76). `prompt status` shows the ruling state
while held; a pending request may be withdrawn with `prompt cancel`. An
in-flight approved release must finish before the posted question can be
cancelled. `--await-answer` uses one timeout for the ruling and answer waits.
Silence or lost advisor authority holds the card. If no authority is configured,
new cards use the audited bypass and publish through the same release path.
Explicit authority disable also publishes pending and newly bypassed cards;
restart recovery retries unfinished publication.

## Progress and decisions

A currently bound lead can send a concise user-directed update through
`assistant publish --publish-kind prose` using its original dispatch and input
IDs. This commits a normal chat event; it does not wake either conversation or
authority backend. The lead should identify the subject in its text. Raw tools
and internal logs remain in the lead's own transcript. Use the conversation
backend for requested synthesis, not as a mandatory relay for each update.

For a blocker or question requiring judgment, the lead first sends the existing
`lane.decision` operation with `transition=wait`, the current lane version and
existing operator-basis IDs. An optional `reason` carries the decision needed,
relevant evidence and recommended next action (nonempty, at most 1,024
characters). It is stored in the immutable receipt and JSON-encoded into the
single existing authority wake. It adds context, never consent. Identical
replays do not wake again; changing the reason under the same key conflicts.

The authority resolves what it can within the existing grant. When the operator
must decide or act, it instructs the current lead to open the existing durable
question in the composite chat. This ordering is a commission/baseline rule;
it does not add a second approval store or change question authorization.
The answer remains bound to the current lead, which continues within the grant.

A lane already in `waiting` cannot transition to `waiting` again. Reconcile the
same receipt for the same blocker; for a distinct decision while waiting, send
one explicit correlated decision notice through the existing peer channel. Do
not manufacture a start/wait cycle. Completion retains the existing one-time
terminal report and authority acceptance path.

Existing owners outside the composite keep their parentage and grants. A report
from a parentless or differently parented owner does not automatically notify
the authority that commissioned it by ordinary send. Such a commission must
request one explicit completion notice with its durable report ID. Routine
progress should remain in the owner's visible chat unless a supported
composite publication binding exists. No duplicate owner or reparenting is
needed solely for presentation.

## Direct replies and strict publication

With strict publication enabled, only an explicit `assistant publish` delivers a
front-desk reply to the operator; turn text is never mirrored. The dispatch packet
supplies `publish:<dispatch>` for a final and `publish:<dispatch>:ack` for an
acknowledgment, and instructs the seat to end its turn with `done`. The original
transcript remains intact. If a landed operator dispatch ends without any
publication, a `dispatch_no_reply` notice immediately wakes the bound seat with
both commands. After 300 seconds without a publication the chat gains one
`unanswered` marker attached to the operator message. Desk-originated dispatches
receive neither replies nor these reminders; use a proactive relay note instead.

`PENTACLE_ASSISTANT_STRICT_PUBLICATION` defaults on for the primary assistant;
`PENTACLE_ASSISTANT_<NAME>_STRICT_PUBLICATION` defaults off for other assistants.
`inspect_stream` exposes the effective value and source. Set the primary kv
`assistant.strict_publication.enabled` to `off` for immediate rollback; named
assistants use `assistant.strict_publication.<name>.enabled`. With strict mode
off the previous mirror and turn-final behavior and untyped proactive publishes
remain available; Daff defaults to that behavior.

## Dispatch batching

An operator input that arrives while the bound seat still has an open direct
dispatch (resolved, not failed, and with no final prose publication yet) keeps
its own dispatch id, envelope and single delivery. Nothing is resent, and no
receipt changes. The daemon records `batched_into_dispatch_id` (the oldest open
dispatch) and appends a batch block to the envelope listing
`batch_head_dispatch_id`, `batch_dispatch_ids` and
`batch_reply_to_message_ids`. Question answers, voice-answer takes and
explicit replies are never batched. An input resolved for a new seat or
generation never joins an older seat's open dispatches.

To answer a batch, publish once under any one of its dispatch ids and pass
every answered input in `--reply-to-message-ids` (including that dispatch's own
`--reply-to-message-id`). Each listed id must be an open dispatch of the same
seat and generation; any other id fails with
`assistant_publish_reply_unverified`. The publication closes every other listed
dispatch with its own closing row (`publication_key=publish:<dispatch>`,
`merged_into_event_id` set to the publication's event). The reply's
`closed_dispatch_ids` lists those dispatches in the order given. The batch gets
one canonical event and one reply push. An input the publication does not list
stays open. The seat answers it under its own dispatch id, and newer inputs
batch into it. Never publish the same body twice.
Publishing a member already closed by a batch under its own dispatch id returns
`assistant_publication_idempotency_conflict`.

## Direct-primary questions

The current direct-primary seat asks with the ordinary durable `prompt ask`,
even while hidden. The daemon surfaces those cards in the composite chat: a
`prompt.list` for the composite stream also covers the bound seat, and each of
its question notifications carries `surfaced_to_stream_id` naming the composite.
The producer and the answer route stay the bound seat. After a rebind the
daemon stops listing and stamping the previous seat's cards; a web client that
was already open keeps its cached cards until it reloads, and answers still go
to that seat. A closed seat's questions expire as usual. A failed binding read
omits the stamp and the wider scope; it never fails hello or a list. Lane
`question.open` is not used in this mode.

## Proactive publications

The current bound seat may publish a `prose` or `status` note with
`assistant publish --proactive`, or `send-file --to <assistant-stream>
--proactive`. With strict publication enabled, pass `--note-type` as `result`,
`problem`, `reminder`, or `relay`, and `--ref <kind>:<id>` naming an existing
`message`, `lane`, `report`, `wake`, or `tell`. A reminder refers to a wake or
message; a relay refers to a tell or message. The publication and chat event
retain both fields. A proactive note whose NFKC-normalized, lower-cased text
matches a publication in the previous 60 minutes after Markdown emphasis and
whitespace normalization is refused with `assistant_publish_duplicate_text`
and the earlier event id. An identical request-key replay returns its receipt.
The daemon verifies the bound seat and its generation on every publish.

## Working with your assistant (coordination practices)

These are the conventions we use to keep an assistant-run workspace calm and productive. They are guidelines, not features — adopt what fits.

- **One ordinary chat.** The assistant is a normal top-level chat — talk to it naturally. A new topic can begin while earlier work continues; it keeps each task's scope and owners intact.
- **Keep the sidebar clean.** Long-running work runs in separate lanes. A lane you started is led by a visible leader you can open and talk to directly; lanes the assistant manages keep their leaders hidden and report through the assistant's chat. The helpers and reviewers under a lane stay hidden and are closed when their work is done, and finished or idle sessions are closed promptly.
- **Decisions as cards.** When the assistant needs a decision from you, it asks with a durable question card carrying the context, what each option does, and a recommendation — so it can still be answered correctly later, not only inline in the moment.
- **Simplest thing that works.** Reuse what already exists, finish the scoped work, and avoid duplicate review/permission chains or temporary patches that leave the real problem unresolved; small leftovers are finished before a task is called done.
- **Verify, don't hand you test steps.** Features are checked with mocks and agent-driven runs; you are asked only for a step that genuinely needs you, such as real hardware or a real account action.
- **Escalate instead of thrashing.** When something fails the same way twice, the assistant freezes retries, gets an independent look, and tells you once with a plan.
- **A second agent for big external work.** You can connect a separate cloud agent (see the Dot connection guide) for large, self-contained tasks, while the local assistant handles day-to-day coordination and small fixes.
- **Wake the assistant only for decisions.** The front desk is woken at once by your messages and answers, `GATE`/`BLOCKER` tells, finished child reports, failed or rejected rulings and the loss of the bound ruling authority; routine START/END tells, receipts and concurs arrive in one hourly digest, and idle notices are not sent. It compacts at 200K tokens, and an unconfirmed compact is retried. See [front desk wake reduction](front_desk_wake_reduction.md).
- **Spend on outcomes, not ceremony.** Cut no-op wake-ups, status relays and rebuilds of finished work, not the work itself. An advisor or external agent stays silent on a plain concur or routine progress and writes only for a rejection, a blocker, a decision or a review-ready result.
- **One true current state.** Each lane keeps a single short current checkpoint that is replaced when reality changes, separating what is in source, deployed, user-proven and remaining; history is linked, not accumulated.
- **Plain, timezone-aware updates.** Status and times are written for you in your own local time, and routine no-op notices stay quiet.

### Conversation visibility at admission

Commission a conversation the operator requested with explicit `--visibility default`, including when its parent is another agent. Commission managed work with `--visibility hidden` and its existing completion policy. Omitted fresh parented work is hidden for every role, including Nexus and QA; fresh top-level creation remains visible. Omitted handoff and native Claude resume inherit stored predecessor visibility; an explicit `default`, `hidden` or `nested` overrides ordinary inheritance. Genuine FD and protected Daff seats remain hidden. Launch instructions and the first durable open use the same effective value. Visibility does not grant lifecycle, prompt or rebind authority.

Inspect the returned stream and its effective visibility before reporting a conversation available. Same-key replay retains the admitted stream, initial presentation decision and delivery receipt; it does not reinterpret a changed predecessor. Legacy cached requests and schedules with explicit `default` retain that choice.

### Supplying assistant guidelines

Guideline delivery is disabled by default. Set
`PENTACLE_ASSISTANT_GUIDELINES_ENABLED=1` in the daemon environment and restart the daemon to enable it;
set it to `0` and restart the daemon to stop delivery. The seats do not need
to be restarted. The daemon resolves the memory repository through
agent-orch's existing `AGENT_ORCH_MEMORY_REPO` or `memory_repo_path` configuration.
`PENTACLE_ASSISTANT_GUIDELINES_FRONT_DESK_FILES` and
`PENTACLE_ASSISTANT_GUIDELINES_ADVISOR_FILES` optionally override the ordered,
colon-separated paths relative to that repository. The defaults are
`agents/assistant_baseline.md:docs/reference/assistant_bart_context.md` for the
front desk and `agents/assistant_authority_baseline.md` for the advisor. Paths
outside the repository are refused. Front matter is stripped before the text is
hashed and delivered; the stamp includes the first 12 characters of the hash.

The bound front desk receives a stamped tell on a new binding, after a context
reading drops below half its previous reading, and at the next reading after the
file contents change. Deliveries are at least ten minutes apart; a suppressed
refresh is retried on a later context reading. The tell wakes the front desk
without waiting for the digest. Before each ruling request or peer tell to the
bound advisor, the daemon sends its current guidelines in a stamped tell. A
missing or unreadable file is logged and retried; it does not block an advisor
request. `inspect` includes `assistant_guidelines`, with the seat, last delivered
identity and time, and whether the identity matches the current files.
