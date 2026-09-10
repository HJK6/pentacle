# Public runtime and support boundary

The public desktop uses the bundled `pentacle-chat-core` reducers, the desktop `ChatStoreController`, and the shared transcript view. `main/chat_stream_client.js` connects directly to the configured daemon; `main.js` adapts its replies to the renderer IPC contracts. There is no in-memory substitute for spawning, sending, naming, or closing sessions. Unavailable services return explicit errors.

The daemon owns tmux session creation and session inventory. The desktop terminal adapter attaches a PTY to an existing daemon session, using local tmux or a configured SSH host. Closing a terminal attachment does not kill the daemon session. The renderer runs bundled local code with Node integration; navigation and new windows from web content are blocked. External HTTP(S) links open in the system browser.

The public dashboard demo is standalone. A private dashboard hub, microphone backend, machine-specific deployment, and organization-specific automation are not included. Their presence in older architecture notes does not supply an integration. Keep their feature flags disabled until you implement/configure their adapters. The public window scheduler supports inline prompts; blob-backed scheduled prompts and installation attestations are explicitly rejected before persistence. Recovered unsupported rows fail closed. Authenticated operator schedule inventory includes bounded inline-prompt previews.

Provider protocol identifiers (`claude`, `fable`, `codex`), model IDs, RPC names, and credential client kinds are wire contracts. Synthetic tests use these real identifiers with invented content, paths and session IDs. Privacy cleanup must not rename one side of a protocol. Usage collectors consume user-supplied collector scripts; the repository does not include account-specific scraping or authentication helpers.

## Validation

`npm test` exercises the real main IPC shape, renderer bundle/store/view, send receipt settlement, shared telemetry, and the existing renderer/main regression suites. `tools/public_desktop_smoke.py` adds a real Electron window, the real daemon accept loop and spawn/send handlers, an isolated tmux server, and a native provider fixture that writes USER and ASSIST JSONL. It verifies actual assistant DOM content and then stops its daemon to verify disconnected sends fail. Cleanup kills only its named tmux server and daemon process group and removes generated credentials; evidence remains in a temporary artifact directory.

`services/chat-stream-v2/tests/smoke/test_public_socket.py` supplies the daemon runner's public socket smoke tier. Default service tests use synthetic fixtures and exclude the long soak mark. Real-provider opt-ins need installed, authenticated CLIs; passing the deterministic fixture does not claim those providers were contacted.

The vendored core corresponds to public `HJK6/pentacle-chat-core` commit `3e19bdd5a03b04cfe86fbcf05fb3d5ed4fc7c8e3`. Its daemon update fixture is byte-identical to the service fixture, and its cursor identities are decoded and checked against their outer parent/child pair.
