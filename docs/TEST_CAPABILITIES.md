# TH-H2 portable daemon gate

## Exact interpreter and bounded execution

From services/chat-stream-v2, create a disposable venv and install the existing requirements:

    python3 -m venv /tmp/pentacle-h2-venv
    /tmp/pentacle-h2-venv/bin/python -m pip install -r requirements.txt
    /tmp/pentacle-h2-venv/bin/python -I -c 'import websockets.asyncio.client, cryptography'

Use that same interpreter for pytest. Missing declared dependencies, a missing pytest-timeout plugin, malformed probe output, unexpected exceptions, and uncertain cleanup are errors everywhere. The consent ceremony retains its isolated -I children; no user-site/PYTHONPATH workaround is used.

The changed surface is test_capabilities.py, test_h2_portable_fixtures.py, test_consent_host_ceremony.py, test_ssh_control_master_reset.py, test_harness_reaping.py, and test_session_attribution_title_parity.py. Also run unchanged test_tmux_isolation.py for the fixture wrapper integration. Run these seven modules with pytest -q -rs; compile with python -m compileall -q tests.

Where the execution shell belongs to process group 1, launch pytest using subprocess.run([sys.executable, '-m', 'pytest', ...], start_new_session=True). This gives the existing own-group rejection test its intended ordinary owned process group; its assertion is unchanged. Do not run against an operator's ambient tmux socket.

Changed tests use existing pytest-timeout: module limits are 20/30/60 seconds, with the existing 60-second hermetic soak bounded at 90 seconds. External probes have separate 2–5-second subprocess limits. Probe targets are disposable children, files, and private tmux sockets. tmux uses an empty config; cleanup uncertainty fails. The fake SSH transport receives a relative short control directory inside pytest's owned working directory; its existing 100-byte path check remains intact.

## Strict fleet gate

Add --strict-capabilities or PENTACLE_TEST_STRICT_CAPABILITIES=1 to the same pytest invocation. Any required capability absence becomes an error. Normal sandbox mode permits only requires(...) skips for actual unavailable capabilities. The changed-node guard rejects direct skips; unrelated existing skips are not rewritten.

The pre-existing test_sigkill_then_reap_manifest_cleans unconditional tracked skip is outside the changed surface and remains byte-for-byte pinned. It is not a new capability skip and is explicitly distinguished in receipts.

Session-end output gives available/absent/error and the exact skipped node IDs per capability. xdist workers return separate receipts; a missing worker receipt cannot produce a successful gate.

| Probe | Owned target | Normal absence | Unexpected error |
| --- | --- | --- | --- |
| ps argv readable | isolated child | requires skip | fail |
| ps birth readable | isolated child, locale-tolerant fields | requires skip | fail |
| process start identity readable | isolated child /proc or ps identity | requires skip | fail |
| lsof descriptor readable | owned child holding owned file; standard-path fallback | requires skip | fail |
| short-path unix-socket directory writable | disposable directory/socket, bounded candidates | requires skip | fail |
| tmux usable | private socket, empty config, owned HOME, verified teardown | requires skip | fail |
| isolated Python dependencies | exact interpreter -I import | always fail | fail |

## Every changed test

Parameterized cases inherit their function's classification. tests/fixtures/h2_capabilities/changed_tests.json is the executable scope. MANIFEST.json pins each authored synthetic fixture, including original assertion metadata. No traffic or real contacts are included.

| Test | Classification and coverage |
| --- | --- |
| test_capabilities.py::test_birth_probe_accepts_non_english_day_month_tokens | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_broken_environment_never_skips_even_when_result_is_absent | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_changed_direct_skip_fails_but_untouched_skip_is_not_rewritten | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_changed_test_skip_calls_are_confined_to_requires | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_command_probe_targets_only_the_owned_pid | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_dead_worker_without_output_records_missing_receipt_and_fails | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_declared_dependencies_use_exact_isolated_interpreter_and_never_absence | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_fixture_manifest_is_explicitly_synthetic_and_hash_pinned | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_lsof_probe_matches_existing_standard_path_fallback | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_missing_binary_is_absent_but_timeout_and_malformed_output_are_errors | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_missing_declared_timeout_plugin_is_an_environment_error | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_original_assertions_and_tracked_skip_are_unchanged | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_parallel_workers_return_capability_receipts | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_probe_registry_caches_true_false_and_error | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_scope_manifest_covers_all_new_portability_tests | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_socket_cleanup_failure_is_error_not_absence | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_tmux_probe_recovers_from_long_first_directory_without_host_paths | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_tmux_probe_retries_permission_denied_directory_after_cleanup | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_tmux_probe_uses_only_its_disposable_socket | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_true_absent_strict_and_error_outcomes | Fixture-backed; no optional host capability required |
| test_capabilities.py::test_unrelated_direct_skip_remains_outside_guard | Fixture-backed; no optional host capability required |
| test_consent_host_ceremony.py::test_isolated_cli_host_ceremony | Environment-required (-I); pure ceremony counterpart in test_h2_portable_fixtures.py |
| test_consent_host_ceremony.py::test_web_gate_daemon_redirects_both_home_resolvers_before_import | Environment-required (-I), deterministic temp-home isolation proof; the separate pure ceremony test covers ceremony logic, not home resolution |
| test_h2_portable_fixtures.py::test_hermetic_daemon_launch_and_owned_shutdown_use_fake_process_boundary | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_in_process_ceremony_uses_synthetic_signatures_without_children | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_owner_signal_cleanup_sequence_with_injected_resources | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_recorded_descendant_tree_and_complete_argv | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_recorded_descriptor_preserves_real_first_bind_parser | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_recorded_identity_reaping_matches_missing_recycled_and_foreign_cases | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_recorded_process_birth_reaches_real_spawn_normalization | Fixture-backed; no optional host capability required |
| test_h2_portable_fixtures.py::test_recording_rejects_missing_identity_and_own_group_with_frozen_processes | Fixture-backed; no optional host capability required |
| test_harness_reaping.py::test_hermetic_soak_child_env_makes_no_remote_calls | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_missing_leader_start_identity_is_not_signalled | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_reap_is_scoped_to_the_calling_run | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_reap_signals_entry_whose_identity_still_matches | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_record_refuses_the_recorders_own_process_group | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_record_refuses_unavailable_leader_start_identity | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_recycled_pgid_with_different_start_time_is_not_signalled | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_harness_reaping.py::test_sigterm_reaps_owned_processes | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_session_attribution_title_parity.py::test_actual_readonly_provider_descriptor_cannot_first_bind | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_session_attribution_title_parity.py::test_actual_writable_provider_descriptor_binds | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_session_attribution_title_parity.py::test_spawn_retains_readable_current_process_birth | Capability-gated additional host variant; counterpart in test_h2_portable_fixtures.py |
| test_ssh_control_master_reset.py::test_additional_owned_unix_socket_capability | Capability-gated additional socket variant; existing injected fake-SSH cases remain portable and capability-probe units exercise synthetic bind/cleanup outcomes |
| test_ssh_control_master_reset.py::test_check_failure_resets_even_when_session_would_succeed | Fixture-backed; no optional host capability required |
| test_ssh_control_master_reset.py::test_control_exit_timeout_still_unlinks_within_bound_and_rebuilds | Fixture-backed; no optional host capability required |
| test_ssh_control_master_reset.py::test_down_host_records_only_fresh_health_and_never_resets | Fixture-backed; no optional host capability required |
| test_ssh_control_master_reset.py::test_fresh_ok_mux_failure_resets_exact_socket_and_next_call_rebuilds | Fixture-backed; no optional host capability required |
| test_ssh_control_master_reset.py::test_no_persistent_master_reachable_host_does_not_churn_or_falsely_reset | Fixture-backed; no optional host capability required |
| test_ssh_control_master_reset.py::test_present_wedged_master_answers_check_but_hangs_sessions_is_killed | Fixture-backed; no optional host capability required |

## Attribution evidence and assumptions

Before repair, the selected ceremony/SSH/reaping baseline had 8 passes and one setup error because module-wide tmux isolation blocked the otherwise portable own-group test. Removing that module-wide requirement exposed the executor's process-group-1 setup, which was corrected by launching pytest in a new owned session without changing the assertion.

A review repair initially placed -f before -L in the tmux wrapper. The unchanged isolation test caught the option-order contract (39 passes, one failure); the wrapper was corrected to retain -L first. No assertion was loosened.

The scoped development gate then passed 85 tests, with three capability skips (two tmux, one Unix socket) and one unchanged tracked skip, in 16.52 seconds (16.875 seconds including launcher). Final-head measurements belong in the PR receipt; these development numbers are not a final-head claim.

The full daemon suite and actual usable-tmux/Unix-socket strict variants are fleet-owned; this environment lacks those capabilities. No production behavior or existing defect is hidden. The additional Unix-socket probe is separate from six always-runnable injected fake-SSH tests.
