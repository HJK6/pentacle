# Codex smoke coverage

The peer-delivery tests and live-Codex command previously documented here belonged to the deleted v1 service and are not present in the v2 test tree. Do not cite or run them as release evidence.

The current automated authority is:

```bash
python3 services/chat-stream-v2/tools/run_gate.py smoke
python3 services/chat-stream-v2/tools/run_gate.py merge
```

The smoke tier exercises real ephemeral v2 daemons and tmux. The merge tier runs unit plus smoke and writes exact-SHA evidence. A focused live spawn/prompt obligation probe exists at `services/chat-stream-v2/harness/probe_spawn_prompt_obligation.py`; it requires `--artifact-dir` and `--release` and should be used only by the workflow that owns those artifacts.

Provider availability, login state, and cross-host SSH remain external prerequisites. A skipped or unrunnable provider probe is not evidence that a Codex delivery path passed.
