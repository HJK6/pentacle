# Deployment packet evidence

These helpers are adopted from the reviewed deployment packet. The caller owns
accepted configuration, preimages, private paths, resource limits and output
receipts. They do not restart a service or grant deployment authority.

- `health-probe.py EXPECTED_SHA [URL]` waits for welcome, the expected runtime SHA
  and pong. Readiness exceptions are caught inside the socket lifetime, so failed
  attempts close normally before retry. Transport failures remain failures.
  `run-health-probe.py EXPECTED_SHA URL ATTEMPTS_OUT` retains every attempt,
  including welcome connection identity when the server supplies it.
- `full-window.py SOURCE OFFSETS END_UTC ATTEMPTS WINDOW_OUT RECEIPT_OUT
  [WATCHDOG_PROOF]` freezes end offsets, streams every appended byte and selects
  boot-to-end records. Start offsets include path, inode, size, captured UTC and
  optionally device. Rotation, truncation, malformed UTF-8, unfinished records,
  records above 1 MiB or more than 100,000 retained diagnostic identities fail
  explicitly. Failure and warning counts cover the whole window; examples cap
  at 100. The selected artifact and its hash retain complete evidence.
- Clean probe closes (1000/1001) need no attribution. An exact welcome `conn_id`
  joins connect and close diagnostics when attribution is needed; time only
  checks consistency. Missing, reused or contradictory identity is unattributed.
  Abnormal closes, genuine classifier failures and incomplete evidence fail the
  bind. No record is excluded, even when an abnormal close belongs to a probe.
- Only the exact missing-watchdog fallback warning can be expected. The optional
  proof names accepted policy, dependency and functioning-polling receipts with
  SHA256s. It requires absent watchdog, no runtime overlay and observed polling.
  Other warnings and missing or contradictory proof fail. Configuration and
  dependency evidence belong to the accepted packet, outside this directory.
- `activation-evidence.py PID PLIST PREIMAGE OUT START RUNTIME MANIFEST` compares
  the installed runtime manifest, config hashes, loaded launch arguments,
  PID/start identity and actual open modules from the intended venv. It accepts
  the resolved framework launcher; another venv sharing that launcher fails.
  Surrounding process-start whitespace is normalized; raw facts are retained.
  `DOMAIN` names the launchd domain. The helper is macOS-specific.
- `assert-no-watchdog.py [OVERLAY...]` records actual dependency absence under the
  selected interpreter. `pin-readback.py --state-root STATE --output OUT` opens
  databases read-only and reports all pin-drift facts plus an explicit census.
  Add `--satellite HOST` for each accepted satellite; fleet identities remain in
  caller configuration.
- `scrub-gate.py SOURCE OUTPUT` invokes the canonical merge gate, scrubbing ambient
  identity and opt-in variables only in the child. It records removed names,
  never values. Run it in the coordinator's granted heavy-gate window.

Focused checks: `python -m pytest tests/test_deploy_packet_activation_evidence.py
 tests/test_deploy_log_guard.py tests/test_deploy_script.py -q -rs` from the service
root. The final candidate still requires the canonical merge gate and independent
review. A welcome/pong probe establishes readiness; activation also requires the
affected feature journey, exact runtime/PID binding and owned cleanup receipts.
