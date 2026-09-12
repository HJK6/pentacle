# Public runtime and support boundary

The public desktop uses the bundled `pentacle-chat-core` reducers, the desktop `ChatStoreController`, and the shared transcript view. `main/chat_stream_client.js` connects directly to the configured daemon; `main.js` adapts its replies to the renderer IPC contracts. There is no in-memory substitute for spawning, sending, naming, or closing sessions. Unavailable services return explicit errors.

The daemon owns tmux session creation and session inventory. The desktop terminal adapter attaches a PTY to an existing daemon session, using local tmux or a configured SSH host. Closing a terminal attachment does not kill the daemon session. The renderer runs bundled local code with Node integration; navigation and new windows from web content are blocked. External HTTP(S) links open in the system browser.

The public dashboard demo is standalone. A private dashboard hub, microphone backend, machine-specific deployment, and organization-specific automation are not included. Their presence in older architecture notes does not supply an integration. Keep their feature flags disabled until you implement/configure their adapters. The public window scheduler supports inline prompts; blob-backed scheduled prompts and installation attestations are explicitly rejected before persistence. Recovered unsupported rows fail closed. Authenticated operator schedule inventory includes bounded inline-prompt previews.

Provider protocol identifiers (`claude`, `fable`, `codex`), model IDs, RPC names, and credential client kinds are wire contracts. Synthetic tests use these real identifiers with invented content, paths and session IDs. Privacy cleanup must not rename one side of a protocol. Usage collectors consume user-supplied collector scripts; the repository does not include account-specific scraping or authentication helpers.

## Validation

Desktop structured Chat is experimental and disabled in the shipped example.
Terminal sessions are the default. The structured-view smoke explicitly opts
in to `features.chatUi`; its passing result does not make that view the default
or establish production readiness for every structured-chat interaction.

`npm test` exercises the real main IPC shape, renderer bundle/store/view, send receipt settlement, shared telemetry, and the existing renderer/main regression suites. `tools/public_desktop_smoke.py` adds a real Electron window, the real daemon accept loop and spawn/send handlers, an isolated tmux server, and a native provider fixture that writes USER and ASSIST JSONL. It verifies actual assistant DOM content and then stops its daemon to verify disconnected sends fail. Cleanup kills only its named tmux server and daemon process group and removes generated credentials; evidence remains in a temporary artifact directory.

`services/chat-stream-v2/tests/smoke/test_public_socket.py` supplies the daemon runner's public socket smoke tier. Default service tests use synthetic fixtures and exclude the long soak mark. Real-provider opt-ins need installed, authenticated CLIs; passing the deterministic fixture does not claim those providers were contacted.

The vendored core corresponds to public `HJK6/pentacle-chat-core` commit `add42ac39facc91af03a25bdbffadd09653e4f74`. Its daemon update fixture is byte-identical to the service fixture, and its cursor identities are decoded and checked against their outer parent/child pair.

## Public identity and fixture policy

Machine identities come from configuration, never a built-in fleet. The CLI uses `AGENT_ORCH_HOST_ID`, then `local_host_id` in its user config, then a sanitized local hostname. Daemon launch uses `--local-host` and the configured machine allowlist. Desktop identity and appearance follow [desktop configuration](desktop_config.md).

Live tools use `PENTACLE_SMOKE_HOSTS` or `PENTACLE_SATELLITE_HOSTS` when supplied; otherwise they read the existing machine configuration (inline `PENTACLE_MACHINES_JSON`, `PENTACLE_MACHINES_FILE`, user machines.json, then a local-only default). Pinning selects remote machines only and refuses an empty target set before opening the Store. Empty or duplicate explicit lists fail. Scheduled smoke follows the same machine configuration. Local tool identity uses `PENTACLE_HOST_ID`, `AGENT_ORCH_HOST_ID`, then the configured local machine.

The auth-context marker is disabled unless `PENTACLE_AUTH_CONTEXT_MARKER_HOST` identifies the host whose provider wrapper emits that marker. Shipped spawn policy applies the common concurrency cap; per-host overrides belong in a deployment-owned `services/_shared/spawn_defaults.local.json` (`{"schema_version": 1, "host_overrides": {...}}`, merged over the shipped `host_overrides`, never committed to the public repository). Satellite service templates read their identity from `PENTACLE_SATELLITE_HOST` in the private satellite environment file.

Fleet CLI installation defaults to the current user's local machine. For multiple hosts, pass `--host-config` with a JSON object mapping names to `ssh` and absolute `release_root`, then select `--hosts` and the local `--run-host`. See [release host example](../configs/fleet_release.example.json). Unknown names and malformed target maps are rejected before staging.

`python3 scripts/check_public_residue.py` scans tracked UTF-8 file contents, including documentation, configuration and the checker itself. [The exact fixture allowlist](../configs/public_fixture_allowlist.json) documents each permitted synthetic test input. Its entries retain anonymizer-era identities only to exercise routing, rendering, protocol compatibility and isolation; they are not deployment defaults. No directory patterns or production exemptions are admitted. Live-provider test admission requires an explicit `PENTACLE_LIVE_TEST_HOSTS` allowlist or the existing deliberate force opt-in. Test resource accounting accepts `PENTACLE_TEST_LOCAL_HOST` as an optional local alias.

Usage updates include `limits_health` alongside `limits`. A changed health record emits an update even when the last valid values remain the same; a healthy probe clears the error through the same path. Invalid state files retain the preceding valid frame and health. The desktop displays the retained values and escaped provider error according to the [limits contract](desktop_config.md).
