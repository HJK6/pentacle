# Developer onboarding

This walkthrough runs Pentacle and the chat daemon locally with disposable state. It is suitable for a fresh checkout and does not require a managed service.

## 1. Install prerequisites

Install Node.js, Python 3, tmux, and a provider CLI that can be replaced by a fixture command in tests. Install dependencies from the repository's documented lockfiles.

```bash
npm ci
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r services/chat-stream-v2/requirements.txt
.venv-dev/bin/pip install -e services/agent-orch
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
  --bind 127.0.0.1 --port 7791 --local-host local \
  --db /tmp/pentacle-example/stores/sessions.sqlite \
  --notifications-db /tmp/pentacle-example/stores/notifications.sqlite \
  --assets-db /tmp/pentacle-example/stores/assets.sqlite \
  --blob-root /tmp/pentacle-example/stores/blobs \
  --spawn-cwd /tmp/pentacle-example/work \
  --projects-root /tmp/pentacle-example/projects
```

Select a local-only machines file explicitly if you already have private fleet
configuration; see [machine configuration](agent_orchestration_setup.md#machines-file).
Keep its name and the daemon `--local-host` identical. The example above uses
`local`. Start with authentication disabled only for local fixture work.

## 4. Check the CLI

Use the CLI installed in the same environment and explicitly match the daemon identity:

```bash
source .venv-dev/bin/activate
export AGENT_ORCH_HOST_ID=local
export AGENT_ORCH_WS_URL=ws://127.0.0.1:7791
export AGENT_ORCH_RUNTIME_DIR=/tmp/pentacle-example/cli
```

Then run:

```bash
agent-orch list
agent-orch reconcile status --json
```

Create one fixture session, send `hello from fixture`, observe the typed reply, and close it. Use generated request ids when scripting the flow.

## 5. Run the desktop

Save this JavaScript module as `/tmp/pentacle-example/pentacle.config.js`.
The desktop connects to the separately started daemon; session working
directories and provider paths belong to the daemon configuration. See
[desktop configuration](desktop_config.md) for the complete supported fields.

```js
module.exports = {
  appName: 'PentacleExample',
  features: { chatUi: true },
  chatStream: {
    url: 'ws://127.0.0.1:7791',
    localHost: 'local',
    hosts: ['local'],
  },
};
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

## 8. Pushing safely

Enable the repository's push guard once per clone:

```bash
git config core.hooksPath scripts/hooks
```

`scripts/hooks/pre-push` then refuses three classes of accident (each overridable with `git push --no-verify` when you are certain):

- **Wrong destination** — a push to a remote *named* `public` whose URL is not the public `HJK6/pentacle`. The resolved URL is printed on every push so you can see where it is going.
- **No explicit refspec** — a bare `git push <remote>` or a matching-branch push. Always name what you push: `git push public my-branch:refs/heads/my-branch`.
- **Foreign history** — a branch whose tip shares no merge-base with the remote's `main` (for example a different repository's line of history). Branch from and rebase on the public `main` before pushing.

Push with an explicit refspec and read the remote back before announcing a push landed:

```bash
git push public my-branch:refs/heads/my-branch
git ls-remote public my-branch
```

Its checks are covered by `test/pre_push_hook.test.js`, which runs under `npm test`.
