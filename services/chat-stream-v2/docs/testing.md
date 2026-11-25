# Test kinds

**Contract.** Add new chat core behavior cases in `tests/contract`. They start
`main.py` as a separate process through the existing restart harness. Assert
public WebSocket frames, the fixture provider's input, transcript and pane,
and the daemon's exit status. Run the daemon with its production composition
and default timing, using the stub Claude binary, a loopback port and owned
paths. The launcher disables host discovery and usage publication because
the fixture contains one host and no usage account. Ingest, session inventory
and reconciliation stay enabled.

Give each case its own HOME, configuration, token and transcript paths and
tmux server. Pass only fixture settings to the daemon and tmux. Use the boot
marker to discover the port, stub control files to stage input and the fixture
seat token to authenticate. Drive the handshake over the socket, wait for
public state with a bound, and release owned resources at teardown. Keep
database reads, private daemon imports, private interfaces and sleeps out of
the cases. New tests exercise the real composition through the daemon entry
point whenever they need a daemon.

**Unit.** Exercise the logic of one module in memory, with no process, socket
or tmux dependency.

**Legacy.** The existing suite remains in its current locations with its
existing gate coverage.

With the [service requirements installed](../README.md#tests-and-supported-integrations),
run the contract tier alone from the repository root:

```sh
python services/chat-stream-v2/tools/run_gate.py contract --unit-workers 2
```

The local merge gate and hosted checks run the contract tier first. During
qualification or review, exercise the seeded faults against a clean committed
tree. The command first runs the unchanged cases and then runs each case
against its fault in a scratch checkout. It retains the test result and
applied diff in a new evidence directory outside the repository.

```sh
python services/chat-stream-v2/tests/contract/faults.py --evidence /path/to/new-evidence-directory
```

## Timeouts

Use watchdogs to stop stuck work. Allow at least three times the slowest healthy
run observed under load. Assert the outcome and allow time for it to arrive.
When a product deadline is part of the behavior, configure a short deadline in
the fixture and leave a generous outer wait for the result. Release checks
measure and report durations so regressions can be reviewed separately.
