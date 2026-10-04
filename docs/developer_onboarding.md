# Developer onboarding

This walkthrough runs Pentacle and the chat daemon locally with disposable state. It is suitable for a fresh checkout and does not require a managed service.

For a persistent named assistant, continue with [the executable bootstrap recipe](assistant.md#bootstrap-your-own-assistant). It uses operator challenge/proof authentication and a protected role; the scratch session below is a separate introductory journey.

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
  --host 127.0.0.1 --port 7791 --local-host local \
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
`local`. Use loopback for scratch work and enroll an operator credential before
UI use; follow [operator authentication](REMOTE_AUTH.md).

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

Create one ordinary session through New Chat, send `hello from fixture`, observe the provider reply, and close it. An enrolled operator credential is required to grant the protected assistant role; an ordinary seat token is insufficient. Use generated request ids when scripting the flow.

## 5. Run the web client

Save this JavaScript module as `/tmp/pentacle-example/pentacle.config.js`.
The web host connects to the separately started daemon; session working
directories and provider paths belong to the daemon configuration. See the
[shared configuration reference](desktop_config.md) for client settings and
the [web host guide](../server/README.md) for server flags and authentication.

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

Build and serve the web client from the repository root:

```sh
npm run build:web
PENTACLE_CONFIG=/tmp/pentacle-example/pentacle.config.js node server --bind 127.0.0.1 --port 7795
```

Open the printed URL in a browser. A browser that supports web-app installation
can add Pentacle to its app launcher; the installed PWA uses the same served web
client and daemon. The Electron desktop app is deprecated and receives no further
upgrades, packaging or rollout. A credentialed daemon requires its explicit
private `chatStream.tokenPath`; keep that file outside the repository and follow
the [web host authentication guide](../server/README.md).

## 6. Test optional adapters

Use `example.local` and `10.0.0.0` only in isolated adapter fixtures. Keep SSH,
WSL, microphone, dashboard and remote-daemon integrations opt-in; see the
[configuration reference](desktop_config.md) for supported feature flags.

## 7. Run validation

```bash
npm test
python3 services/chat-stream-v2/tools/run_gate.py unit
```

Use the activated environment from step 4, or its explicit `.venv-dev/bin/python`.
For the standard Web gate, run `npm run build:web` then
`node test/e2e/web_gate.js --python .venv-dev/bin/python`; the system Python
may not have the installed daemon dependencies.

Evidence should contain only synthetic fixture ids, result summaries, and the candidate identifier. Remove temporary stores after the run.

The [own-assistant gate](../test/e2e/own_assistant_bootstrap_gate.py) invokes the actual bootstrap with fresh state/registry, unique tmux socket, native-format provider counterpart, candidate publish CLI and real Chrome browser. After dependencies and the Web build:

```sh
python3 test/e2e/own_assistant_bootstrap_gate.py --output-dir /tmp/pentacle-own-assistant-proof
```

The output directory must be fresh. Set `PENTACLE_TEST_BROWSER` to Chrome/Chromium if public detection cannot find it. The gate does not overwrite HOME or CODEX_HOME, inherit caller/production tokens/endpoints, install services or contact paid providers. It checks a correlated visible reply, name, exact backend generation, duplicate publication, stale-generation refusal and restart readback. It owns daemon/web/browser/tmux cleanup and removes plaintext fixture credentials in `finally`; retain raw failure receipts for classification. PASS proves installation mechanics, not provider login, real model availability, physical phone or microphone hardware.

## 8. Pushing safely

Enable the repository's push guard once per clone. `core.hooksPath` is a
**repo-wide** setting shared by every worktree, and a **relative** value silently
runs nothing in any worktree or branch that does not contain the file — for
example a checkout parked on an unrelated branch — so a foreign push sails
through with no output. Point it at an **absolute** path so it is active from
every worktree and branch:

```bash
git config core.hooksPath "$(git rev-parse --show-toplevel)/scripts/hooks"
```

If some worktrees track branches that do not carry the hook, copy `scripts/hooks/`
to a stable location outside the tree and point `core.hooksPath` there instead,
kept in sync with public `main`.

Verify the guard is actually active with a dry-run foreign-history probe — it
must be REFUSED, not silently pass (`git push --dry-run` still runs pre-push):

```bash
# from a branch whose history is unrelated to public main
git push --dry-run public HEAD:refs/heads/tmp-foreign-probe
# expect: "pre-push: REFUSED: … foreign history …" and a non-zero exit
```

`scripts/hooks/pre-push` then refuses accidental pushes. A refusal is a finding to resolve before retrying. Exactly what it protects:

| Situation | Result |
|---|---|
| Remote *named* `public` whose URL is not `github.com/HJK6/pentacle` | **REJECTED** (destination) |
| Any push whose URL is `github.com/HJK6/pentacle` — under any remote name (`public`, `origin`) or a raw URL | Public rules applied (resolved URL printed) |
| Other or ambiguous `github.com` destinations | Public safeguards applied, failing closed; only the exact private repository receives private-root privileges |
| Bare `git push <remote>` (no refspec) or a matching-branch push | **REJECTED** (name what you push) |
| `git push <remote> my-branch` (same-name branch) | **Allowed** — maps to the same name; this check does *not* force `src:refs/heads/dst`, so a wrong *branch name* is out of scope (content is still checked below) |
| A tip that reaches a root commit which is not a root of the remote's `main` — a foreign orphan branch, **or a merge that pulls another repository's history into a clean branch** | **REJECTED** (foreign ancestry) |
| Destination is the private line `github.com/HJK6/pentacle-private` (any remote name) | Same root-set check, but the public repo's root is additionally **allowed** — the private `main` merges public `main` back by design; any other new root is still **REJECTED**. _(2026-10-04: `pentacle-private` is retired/archived read-only; this merge-back path is historical. The rule is retained so the guard still fails closed on any such destination.)_ |
| GitHub destination (including private) whose `main` cannot be resolved (no ref, fetch fails) | **REJECTED** (fail closed) |
| Non-GitHub destination without private test/mirror classification and no resolvable `main` | Allowed with a note (nothing to compare) |

The foreign-ancestry check is a root-set comparison, not a shared-ancestor test: a merge of unrelated history still shares a merge-base with `main`, so `git merge-base` is not enough. The explicit-refspec check reads the invoking `git push` from `/proc` (Linux) or `ps` (macOS/BSD). Git for Windows runs the hook in MSYS with no useful `$PPID`, so its checked-in PowerShell helper reads the native `git.exe` process ancestry and tokenizes that command line. If the argv cannot be verified on any platform, a push to the public repo fails closed.

Push with an explicit refspec and read the remote back before announcing a push landed:

```bash
git push public my-branch:refs/heads/my-branch
git ls-remote public my-branch
```

Its behavior is covered by `test/pre_push_hook.test.js`, which runs under `npm test`.
