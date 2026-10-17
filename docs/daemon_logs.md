# Managed daemon logs

The daemon can own two rotating UTF-8 text sinks for normal Python stdout and
stderr. This is opt-in at startup; the default and satellite keep their existing
streams. Importing the module does not change streams. An embedding application
that already owns the root logger keeps its handlers, levels and streams.

Set both absolute paths in the daemon's launch environment:

```text
PENTACLE_DAEMON_STDOUT_PATH=/path/to/owned/logs/daemon.out
PENTACLE_DAEMON_STDERR_PATH=/path/to/owned/logs/daemon.err
PENTACLE_DAEMON_LOG_MAX_BYTES=16777216
PENTACLE_DAEMON_LOG_BACKUPS=3
```

The directory must already exist, belong to the daemon user and prohibit group
or other writes. One daemon owns the directory; do not run external rotation or
another writer against these files. Every active and backup pathname must be
separate from the other stream and the inherited emergency targets. Startup
rejects symlinks, nonregular files, existing hard links/aliases, overlapping
rotation names, oversized existing files and numeric generations outside the
configured backup range. Validation precedes opening either sink. Partial path
configuration or invalid limits fail startup. Set `max_bytes >= 4` and
`backups >= 1`; zero backups does not disable rotation.

Each stream has its active file and `.1` through `.3` by default, newest to
oldest. Every data file is at most 16 MiB, so the two streams retain at most
128 MiB across eight files. The byte limit includes encoded Unicode. Oversized
writes split at UTF-8 character boundaries; the writer does not accumulate an
unbounded buffer waiting for a newline. Concatenate generations oldest first to
reconstruct retained output. Retention removes old generations: the retained
bytes form the newest contiguous suffix, which can start inside an old record.
There is no minimum retention duration or ordering between stdout and stderr.
Normal logging retains its UTC timestamps, logger names, levels and tracebacks.

Python `print`, normal stderr writes, logging and Python tracebacks after setup
use these sinks. Raw OS descriptors 1/2 remain emergency outputs owned by the
service manager. Native/fatal output, pre-setup failures, saved original streams
and subprocesses explicitly inheriting those descriptors remain outside the
managed byte bound. `fileno()` deliberately refers to the inherited descriptor.
The daemon's normal subprocess transports capture output; machine-sampler
command failures are captured and reported through logging. This feature does
not impose a lifetime cap on emergency files or delete existing logs.

## Reads and failures

Deployment reads use the managed path pair when configured. Before activation,
create both empty managed files so the deployment tool can capture a start
inode/device/offset. Keep the previous `PENTACLE_LOG_PATH` and launchd paths as
rollback/emergency configuration; a managed pair takes precedence for readers.
Boot readiness follows a captured inode through retained generations and only
accepts a listening marker after that boundary. Complete-window scans join
record fragments across generations. Missing/expired boundaries, inconsistent
rotation, incomplete records or invalid encoding fail closed. A high-volume
window can expire before a reader completes; it must not be certified as green.

The original boot marker eventually expires. Continuing process identity comes
from the existing lifecycle and watchdog snapshots, together with the served
source identity. Do not replay a retained old marker to claim a new startup.

Startup-open failures stop startup. Runtime write/rotation failures increment
`daemon.stats.stats.managed_logs.stdout.failed_writes` or the corresponding
`stderr` counter, with a bounded `last_error` class. These fields appear only
when managed sinks are installed and remain cumulative for that process. A
fixed, payload-free `managed_log_error` notice reaches inherited stderr at most
once per sink per minute. No unbounded fallback or recursive logging is used;
failed writes can lose diagnostics and are never lossless evidence. Healthy
rotation preserves writes until retention expiry. Indefinite full storage or
blocked filesystem IO cannot guarantee lossless output and responsive service.

## Activation and legacy evidence

Source readiness does not activate a running daemon. The release owner stages
this candidate in a separately authorized combined deployment, preserving the
source/configuration/plist preimages and all legacy logs. Prepare managed files
and capture offsets before restart. Read back the configured limits, exact
served candidate, new PID/boot identity, healthy independent checker samples and
zero managed failure counters. Prove ordinary rotation first in the isolated
fixture; observe real runtime retention without flooding production.

The old large emergency stderr file has a separate post-restart disposition.
Prefer distinct emergency paths for the new process, then prove that no daemon,
child or other writer still holds the old inode before immutable archival with
size/hash evidence. Restart alone does not prove quiescence: the new process may
still inherit that same emergency file. Truncation requires separate authority,
verified archival and all-writer quiescence. Otherwise preserve the old file and
leave its disposition pending. Never use live copy-truncate as a lossless step.

Rollback disables both managed path variables and restores the approved source
and launch configuration in an authorized restart. Preserve managed and legacy
files as evidence; rollback does not overwrite or delete diagnostics.
