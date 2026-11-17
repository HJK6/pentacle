# Attempt-owned SQLite gate guard

The merge gate activates an owned-venv `.pth` hook before Python preflight or
subject execution. `-I -B`, `-c`, `-m`, plain `sys.executable`, changed cwd and
cleared environment all load the hook. The hook reads its configuration beside
it inside the venv, not from `PYTHONPATH` or an environment variable. Startup
failure exits 86; site cannot merely print a warning and continue unguarded.
The bootstrap is idempotent because site can process a venv directory twice.

Every prepared-cache user holds an exclusive advisory lock through the run.
Preparation writes an inactive hook; only a merge run activates the attempt
root. Configuration is deactivated before releasing the lock. An interrupted
run with a stale active hook fails closed before any dependency probe or rebuild;
resolve the prior attempt and its owned descendants before reusing the cache. Prepared-cache files are not a
security boundary against malicious code running as the same OS user.

## Child policy and limitations

Python starts are **covered** only when the executable is inside the exact
prepared venv/bin directory and resolves to its interpreter. `-S` (also combined
flags) is **refused** before spawn, because it bypasses site. Another interpreter,
shell, tmux, node, sqlite3 CLI and opaque native tools are **refused**, not skipped.
The policy also checks `os.exec`/`os.posix_spawn` and refuses `os.system`.
Only system ps/lsof/uname and direct system git rev-parse are classified
**impossible DB openers**; arbitrary git commands are refused because hooks or
external tools can escape the Python envelope.

This intentionally means stock tests requiring tmux or shell/native helpers
can make the merge gate **incomplete**. This change does not claim a passing
full merge gate. Supporting those launch paths requires a separately reviewed
contained launcher or OS confinement. No daemon or deploy product code changes.
This is a guard against accidental database access, not adversarial Python
that rewrites the hook/registry or loads arbitrary native SQLite via ctypes.

`EVIDENCE/gate/sqlite-guard.json` records module hash, venv hook path, attempt
root, registry hash and refusal count. The ledger and refusal journal are
copied beside it before scratch cleanup. Any refusal, including one swallowed
by a test, forces `incomplete`, never green. Bootstrap failures exit nonzero.

## SQLite checks

The shared module is extracted from read_timing, not copied; its standalone
legacy owned-directory behavior remains unchanged. Attempt mode reserves each
fresh file with O_EXCL before SQLite sees it, then appends realpath/device/inode
to the locked, append-only creation ledger. Existing files are never adopted;
reopening requires an exact identity match and a single-link regular file.
Cached statements are reauthorized on each execution. Bound targets must be
exact unadapted strings/bytes and are checked before execution. Nested
read_timing installation keeps the attempt ledger and can only narrow its root.
The boundary does not protect against adversarial same-user renames between
validation and open, ledger rewriting, or arbitrary native-library loading.

## Prominent database-capable launch paths

| Launch | Classification | Reason |
|---|---|---|
| tools/read_timing.py isolated `python -I -B --child` | Covered if prepared venv Python, otherwise refused | .pth loads even in isolated mode |
| tools/run_gate.py pytest subprocess via sys.executable | Covered | Same prepared interpreter and hook |
| deploy/coordinated_harness.py:113 helper `executor["python"] -m` | Covered if prepared venv Python, otherwise refused | Executable/argv0 and prefix checked before launch |
| deploy/coordinated_harness.py:247 daemon `sys.executable -m` | Covered | Same prepared interpreter and hook |
| tests/conftest.py:129 tmux wrapper and all tmux test session starts | Refused | Server/shell descendants cannot be proved to load hook |
| tools/read_timing.py git `-C` candidate inspections | Refused | No broad git execution allowance |
| coordinated_harness.py git archive/commit/setup helpers | Refused | Opaque helper/native launch envelope |
| run_gate.py:77 and coordinated_harness.py:128 direct system git rev-parse | Impossible DB opener | Narrow observation command without hook execution |
| Python `-S`, including combined flags | Refused | Site hook disabled |
| node/sqlite3 CLI, shell scripts and other interpreters anywhere in inventory | Refused | No unguarded native database opener allowed |
| multiprocessing/fork within covered Python | Covered | Spawn uses checked interpreter; fork inherits hooks |

## Reachable launch-site inventory

Static AST inventory of direct subprocess/asyncio/OS launch calls in gate tests,
tools and coordinated_harness. For **each listed site**, runtime argv is
classified by the policy above: prepared-venv Python covered, the narrow
observation whitelist impossible, all remaining launches refused. Dynamic
wrapper calls converge on the same process-wide audit hook. In particular,
read_timing's isolated child is covered; coordinated_harness controller/daemon
children are covered only when they use that same prepared interpreter;
tmux-launched children are refused at the tmux launch, before any session starts.

| Source | Direct launch lines | Classification |
|---|---|---|
| `services/chat-stream-v2/tests/conftest.py` | 129 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/cosmo_e2e/run.py` | 46, 53 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/helpers/u4_process_harness.py` | 272, 407 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/ordinary_recovery_fixture.py` | 11 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/repin_matrix_runner.py` | 61, 62, 63, 64 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/soak/harness.py` | 91, 220, 238, 358 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/soak/restart_matrix.py` | 113, 578, 678, 953 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/soak/test_assistant_auto_restore.py` | 810 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_account_choice_journey.py` | 45 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_account_launch_environment.py` | 32, 67 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_account_observation_freshness.py` | 177, 178 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_agent_orch_path.py` | 144, 192, 233, 298, 342, 389 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_attachment_io_responsiveness.py` | 43 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_authority_restore_startup.py` | 29 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_boot_marker_readiness.py` | 55 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_client_contract_probe.py` | 420, 459, 508, 611 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_close_ladder.py` | 275, 1299 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_close_progeny_cli.py` | 41 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_close_races_spawn.py` | 95, 96, 97, 102 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_close_restart_journey.py` | 71 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_codex_capacity_native_journey.py` | 57, 115 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_codex_capacity_probe.py` | 27, 30, 32, 54 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_codex_usage_collector_e2e.py` | 152 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_consent_host_ceremony.py` | 78, 112, 176 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_cursor_provider.py` | 278, 307, 396 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_daemon_service_install.py` | 132, 385, 398 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_account_selection.py` | 70, 171 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_checkout_switch.py` | 25, 71 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_coordinated_helpers.py` | 327, 328, 329, 645, 649, 680, 926, 927, 971, 1019 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_direct_prepare_import.py` | 80 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_direct_script.py` | 36 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_log_guard.py` | 63, 187, 191, 259, 273 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_ordinary_path.py` | 49 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_ordinary_provenance.py` | 170, 202, 222 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_ordinary_recovery.py` | 19, 115 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_packet_activation_evidence.py` | 138, 164, 202 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_process_provenance.py` | 44 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_deploy_script.py` | 417 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_disposable_smoke.py` | 40 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_exchange_repair_checkpoint.py` | 481, 600 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_fd_close_visible_socket.py` | 80, 183 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_gate_env_selftest.py` | 43, 55 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_gate_progress.py` | 61, 94 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_gemini_provider.py` | 104 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_harness_reaping.py` | 105, 153, 261, 283, 324, 438, 683, 784 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_intake_resume.py` | 34, 511, 518 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_lane_execution_contract.py` | 323 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_launch_native_prompt_delimiter.py` | 53 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_logging_diagnostics.py` | 50 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_loop_stall_process.py` | 50 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_loopback_privileged_rpc.py` | 191 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_managed_logs.py` | 271, 348 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_merlin_claude_auth_context.py` | 66 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_native_trust_recovery.py` | 29 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_nightly_journey.py` | 205 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_primary_assistant_name.py` | 27 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_process_ownership_semantics.py` | 24, 32, 70, 118 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_process_reap_semantics.py` | 139, 149 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_provider_account_host.py` | 81 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_provider_account_native.py` | 297 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_provider_account_operator_login.py` | 48, 159, 188, 228 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_public_claude_usage.py` | 180, 347, 659 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_public_host_configuration.py` | 62, 74, 78 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_qa_attestation_validation.py` | 557 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_queued_send_now_probe.py` | 103 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_raw_command_path.py` | 119, 169, 215, 236 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_read_timing.py` | 30 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_report_variants.py` | 84, 85, 86, 91 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_restart_adoption_deadline_race.py` | 101 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_restart_input_residuals.py` | 158, 494 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_restart_preimage.py` | 1121 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_run_gate_failure_evidence.py` | 91, 206, 237, 305, 322 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_satellite_ack_backoff.py` | 447, 465, 467, 569, 574 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_seat_token_telemetry.py` | 75, 81 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_session_attribution_title_parity.py` | 106, 110, 118, 182 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_soak_regressions.py` | 51 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_soak_sampler_integration.py` | 20 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_source_integrity.py` | 12, 28 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_spawn_resume.py` | 227 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_sqlite_guard.py` | 57, 62 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_store_stop_deadline.py` | 696, 713 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_test_prompt_consumers.py` | 57 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_tmux_isolation.py` | 25, 26, 46 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_tmux_observation_delimiter.py` | 138 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_usage_manifest_deployment.py` | 24, 77 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_usage_observation_tuple.py` | 350 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_usage_provenance.py` | 723 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_lane_confirmation.py` | 51 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_lane_fd_override.py` | 48 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_lane_members.py` | 39 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_lane_progress_socket.py` | 28 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_lane_remaining_estimate.py` | 116 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_owed.py` | 466 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_report_get.py` | 84 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tests/test_work_unattended.py` | 245, 263 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/busy_seat_send_probe.py` | 55 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/event_push_pin.py` | 213 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/household_due_reminders.py` | 218 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/live_window/core.py` | 866 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/nightly_journey.py` | 47 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/orphan_reaper.py` | 477 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/provider_wrapper_probe.py` | 342, 343, 351 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/read_timing.py` | 410, 451 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/run_gate.py` | 77, 209, 341 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/scheduled_admission.py` | 84, 111, 150 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/source_integrity.py` | 12 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/spawn_fleet_smoke.py` | 92 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/tools/usage_manifest.py` | 70 | Covered / impossible / refused by exact runtime argv policy |
| `services/chat-stream-v2/deploy/coordinated_harness.py` | 113, 128, 167, 247, 263 | Covered / impossible / refused by exact runtime argv policy |
