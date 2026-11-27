# Plain release recovery rehearsal

Run only in an isolated test window, with no concurrent live release. This
rehearsal starts a throwaway daemon; it must never run against the serving
label, checkout, databases or progress file.

1. Record the live daemon PID and boot ID. Resolve and record a distinct
   throwaway launchd label, Git checkout, loopback port, data directory, log
   directory and `PENTACLE_DAEMON_PROGRESS_PATH`; stop if any matches live.
   Clear the inherited environment. Use a private home and `--tmux-bin` pointing
   to a wrapper for a private socket, `--claude-bin tests/soak/restart_stub_claude.py`,
   and a local-only `PENTACLE_MACHINES_FILE`. Keep fixture data and logs owned.
   Pass `--disable-mirror --disable-hosts --disable-reconciler --disable-retention
   --disable-nudges --disable-notification-expiry --disable-remote-presence
   --disable-event-push-ingest --disable-routing-integrity --disable-outbound-notices
   --disable-awaiter-resolution --disable-window-schedule --disable-usage-state-publisher`.
2. Use a private local Git origin and checkout. From a serving prior, make an
   unpushed target that starts the watchdog and exits before bootstrap welcome.
   Make the prior fail at the same point only when an owned sentinel exists.
   Run with process access; the sandbox blocks launchd bootstrap. Boot out any
   leftover job only after verifying it belongs to this fixture label, then
   bootstrap the isolated job and verify that the prior serves. Set the
   four `PENTACLE_RELEASE_*` settings required by `plain_release.sh` and a
   separate record directory. Never configure a real probe target.
3. Release the failing target. Require exit 4, `PRIOR RESTORED`, and the prior
   SHA in the welcome. Send once over the restored daemon's socket using the
   disposable provider and the assertions in `tests/contract/test_send.py`:
   landed receipt, exactly one provider input and transcript record, and the
   matching durable USER event.
4. Create the owned sentinel while the prior is serving, then release the same
   target again. Sample the isolated progress file once a second for 60 seconds
   during this run. Require exit 5 and a `needs_human` release record. Replay the
   captured samples through the installed checker's engine and admission policy;
   report the observed boot IDs and admissions, and the observation duration.
5. Boot out only the isolated label, stop its private tmux server, and verify
   the label is absent. Use `lsof -nP -iTCP:<port> -sTCP:LISTEN` to verify that
   nothing listens on the fixture port; a plain bind can fail due to TIME_WAIT.
   Remove the owned plist and fixture
   directories. Read the live PID and boot ID again; both must match the initial
   record. Keep the release output, send proof, samples, replay and cleanup
   readbacks outside the checkout.

An exit 5 reaches the release runner; the script does not itself notify an
operator. A progress sample or welcome frame alone does not prove delivery.
