# Front-desk restart notice

Every chat_streamd v2 process that starts with the primary assistant composite
enabled sends one wake notice to the bound front desk. The notice says that a
restart happened and lists the work in progress at that moment, so the front
desk can resume from one message instead of piecing together `session_dead`
alerts. Source: `restart_notice.py`; hook in `main.py` after
`configure_error_alerts`.

## When it runs

The hook captures a snapshot (open `sessions` rows, the composite binding and
the advisor stream from kv `assistant.authority.shape`) and schedules
`RestartNotice.run_once` as a daemon-owned background task. Nothing on the
startup path waits for a probe or a delivery, and the background workers start
immediately afterwards. `run_once` probes each captured seat once with
`session_state` (local tmux, or `hosts.tmux_for(host)` for a peer). It runs at
most 8 probes at a time, and each probe has a 5 s `asyncio.wait_for`. A timeout,
a transport error or `unreachable` all read as `unknown`. It then composes the
text and delivers it.

## Marker and classification

kv `daemon.restart_notice.last` holds
`{instance_id, pid, started_at, boot_time, daemon_sha, notice_tell_id, kind, error?}`.
It is written once per process, after the notice has been queued durably or
handed off as committed. A delivery or storage failure writes the marker with
`error`; that marker records an attempt, not a delivery. `boot_time` comes from
`sysctl -n kern.boottime` (Linux: `/proc/stat` `btime`), `daemon_sha` from
`event_push.daemon_sha` and `instance_id` from `DaemonLifecycle.instance_id`.
A second `run_once` in the same process, or a marker that already carries this
`instance_id`, does nothing.

The current start is compared with the previous marker; the first matching rule
wins:

| Previous marker | Kind |
|---|---|
| none | `first_instrumented_start` |
| `boot_time` differs by more than 2 s | `host_boot` |
| `daemon_sha` differs (both known) | `deploy` |
| otherwise | `daemon_restart` (crash, stall-checker kickstart or manual restart; these cannot be told apart) |

The first start of a build that contains this code has no marker, so it reads
`first_instrumented_start`. The deployment after that one is the first that can
read `deploy`. Production marker state is never seeded.

## Delivery

The tell id is `restart-notice:<instance_id>` and the sender is
`daemon:restart-notice`. The notice uses the restore notice's delivery
(`_deliver_composite_notice`, which backs `_deliver_restore_notice`). An
unbound or stale binding, a row that is closed or `presumed_dead_at`, or a
delivery `VerbError` such as a dead pane all queue the tell in
`v2_assistant_composite_tell_queue`. The existing restore path (`_complete_routing`)
or rebind path (`_on_assistant_rebind`) later flushes it to the next bound
seat. A reply that is neither committed nor durably held by the digest
does not count as received, and the tell is queued as well. The first line starts with
`BLOCKER restart from daemon:`, so `FrontDeskDigest.ingress` passes it as a wake
without any change to the digest.

Where to read it back: `v2_assistant_composite_tell_queue` (`tell_id LIKE
'restart-notice:%'`) while it is queued, then `v2_tell_deliveries` and the front-desk
transcript after the flush.

Known limits, none of them retried: a process that dies before the hook sends
nothing; a crash between the handoff and the marker write makes the next start
send its own notice; a failure while composing still sends the header line.

## Content

Plain text, at most 6000 bytes, sections in this order, each present (`none`
when empty):

1. Header: kind, host boot, daemon start and SHA, previous start (gap in s) and SHA, or `no previous marker`; then a line explaining the kind.
2. Front desk: binding stream, generation, pane state, and whether an auto-restore episode is expected (`gone` with `PENTACLE_ASSISTANT_AUTO_RESTORE=1`).
3. Advisor: authority stream and pane state, or `none configured`.
4. Seats open at start (excluding front desk, advisor and composite projections): a count line, then at most 20 lines ordered gone, unknown, alive (oldest first in each group), then `+N more: agent-orch list` and the provisional-`gone` note.
5. Print work: open seats whose `spec_id`/`qualified_spec_ids` match `^spec_printer__` (all of them, not only the 20 listed above), at most 8, then the printer check command. The daemon never probes the printer.
6. Windows: active spawn-admission freezes, `holds` and screen leases, at most 8 of each.
7. Advisor rulings: `pending` rows (at most 8), then counts for the other states that are not `done`.
8. Operator questions, across the whole daemon: open `agent_questions` whose producer is still current (the stale-question policy, applied read-only), plus held question rulings (`pending`/`approved`/`bypassed`/`unruled_disabled`), deduplicated by `question_id`, at most 8.
9. Undelivered front-desk traffic: `v2_outbound_notices` counts per kind and the composite queue depth before this notice.
10. Checkpoint: path, size and mtime, or `missing`. The file's contents are never copied.
11. Resume commands.

If the per-list caps still leave the text above 6000 bytes, these lists are
replaced by their count and overflow command, in this order: seats, questions,
rulings, windows, print work, then the undelivered per-kind detail. The header,
front desk, advisor, checkpoint and resume lines are never cut.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `PENTACLE_RESTART_NOTICE` | `1` | `0` skips the snapshot, compose, delivery and marker. Any other value fails startup. |
| `PENTACLE_FD_CHECKPOINT_PATH` | `~/agent-workspace/agent-outputs/fd-checkpoint/checkpoint-current.md` | Checkpoint path reported in section 10. |
| `PENTACLE_RESTART_NOTICE_PRINTER_CMD` | a pointer to `printer status` in `docs/abilities/printer_quest.md` | Command named in section 5; a host sets the literal command here (fleet hosts stay out of this public tree). |

Rollback: set `PENTACLE_RESTART_NOTICE=0` in the LaunchAgent plist and
`launchctl bootout`/`bootstrap` the daemon, or revert. The marker left in kv is
then never read.

## Verification

- Unit: `tests/test_restart_notice.py`.
- Real process: `RESTART_MATRIX_EVIDENCE=<dir> PENTACLE_FORCE_LIVE_DAEMON=1 python3 -m pytest tests/soak/test_restart_notice.py -q`. Cell A SIGKILLs the daemon after killing one child pane (`daemon_restart`, child listed `gone`). Cell B rewrites the isolated marker's SHA as a stand-in for a deployment (`deploy`).
