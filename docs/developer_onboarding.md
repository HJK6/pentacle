# Developer onboarding

This walkthrough runs Pentacle and the chat daemon locally with disposable state. It is suitable for a fresh checkout and does not require a managed service.

## 1. Install prerequisites

Install Node.js, Python 3, tmux, and a provider CLI that can be replaced by a fixture command in tests. Install dependencies from the repository's documented lockfiles.

```bash
npm install
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r services/chat-stream-v2/requirements.txt
```

## 2. Prepare a scratch directory

```bash
mkdir -p /tmp/pentacle-example/{work,stores,projects}
```

Point the daemon's session, notification, asset, and blob stores at that directory. Do not use a release checkout as a mutable runtime directory, and do not commit the scratch files.

## 3. Start a local daemon

Use the daemon's `--help` output as the option authority. A representative loopback invocation is:

```bash
.venv-dev/bin/python services/chat-stream-v2/main.py \
  --bind 127.0.0.1 --port 7791 \
  --db /tmp/pentacle-example/stores/sessions.sqlite
```

If the implementation uses separate store flags, pass matching paths under `/tmp/pentacle-example/stores`. Start with authentication disabled only for local fixture work.

## 4. Check the CLI

Install the CLI in the same environment and use a local configuration with `local_host_id: "coordinator"` and `ws://127.0.0.1:7791`. Then run:

```bash
agent-orch list
agent-orch reconcile status --json
```

Create one fixture session, send `hello from fixture`, observe the typed reply, and close it. Use generated request ids when scripting the flow.

## 5. Run the desktop

Copy `pentacle.config.example.js` to a scratch config and set:

```js
appName: 'PentacleExample',
workingDirectory: '/tmp/pentacle-example/work',
features: { chatUi: true },
chatStream: { url: 'ws://127.0.0.1:7791', autoStart: false },
```

Start with `PENTACLE_CONFIG=/tmp/pentacle-example/pentacle.config.js npm start`. The desktop should show the synthetic session and render a bounded fixture response. A credential file, if enabled by a local test, belongs outside the repository, is owner-readable only, and contains a placeholder or generated value rather than a committed secret.

## 6. Test optional adapters

Use `example.local` and `10.0.0.0` only in isolated adapter fixtures. Keep SSH, WSL, microphone, dashboard, and remote-daemon tests opt-in. Their failures should appear as typed degraded states without preventing the core local chat flow.

## 7. Run validation

```bash
npm test
python3 services/chat-stream-v2/tools/run_gate.py unit
```

Evidence should contain only synthetic fixture ids, result summaries, and the candidate identifier. Remove temporary stores after the run.
