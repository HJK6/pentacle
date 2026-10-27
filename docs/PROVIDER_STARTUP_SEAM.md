# Provider startup seam

Use deterministic replay for account-notification changes:

```sh
python3 scripts/gate_env.py run --profile pentacle --workspace /absolute/owned-gate-env \
  --evidence-dir /absolute/evidence --target v2-pytest \
  --select tests/test_provider_account_native.py \
  --select tests/test_provider_startup_smoke.py \
  --select tests/test_fleet_smoke_provider_errors.py \
  --spec SPEC --series SERIES --purpose acceptance --attempt-manifest MANIFEST
```

The native fixture runs the real RPC transport against an owned subprocess.
It covers initialize-phase provisional null announcements, authoritative-read
malformation refusal, delayed/repeated/conflicting account status, timeout,
process exit and child/stream cleanup. Initialize announcements do not establish
identity; authoritative malformed account status still refuses. The bridge tests
add executable/config drift, native owner mismatch, spawn attribution and refusal
before send. These tests do not authenticate a real provider.

For a separately authorized installed Codex check, use the existing smoke
runner with **both** positionals and the two explicit input paths:

```sh
python3 services/chat-stream-v2/tools/spawn_fleet_smoke.py \
  ws://127.0.0.1:SCRATCH_PORT LOCAL_HOST \
  --token-path /absolute/scratch/operator.token \
  --provider-tuple /absolute/private/provider-tuple.json
```

The tuple file has exactly four fields: `binding` is the existing account
tooling's public Binding receipt with `default_revision: 0`, `config_sha256`
is the SHA256 of that home's `config.toml`, and `model` / `effort` pin the
tested launch. Do not put credentials, email addresses, native output or config
contents in it. The host must match the configured local machine. The CLI
rejects incomplete/ambiguous selection before opening a connection and runs
only that host's Codex promptless cell. No-argument invocation retains the
scheduled full-fleet behavior; it is unsuitable for this bounded check.

Before invocation, the resource owner must provide an already provisioned
account home/config under an explicit exclusive window and an owned candidate daemon. Reuse the scratch
auth/store/port/tmux launch pattern in `tools/public_desktop_smoke.py`, replacing
its provider fixture with the provisioned binding. Configure only the tested
Codex binding in the scratch account policy and set its scratch default through
the existing account/store tooling. No production configuration or default is
changed. The smoke verifies the selected default instead of sending an account
override, preserving the existing front-desk-only account-choice guard.

Freeze the exact candidate source/tree digest, launch command, endpoint,
runtime PID/start identity, daemon configuration fingerprint, credential path,
provider tuple and owned cleanup before the resource window. The caller owns
this candidate attribution; pointing at an installed production daemon proves
only that installed runtime. Use private tmux/socket/database paths and retain
the existing generation-fenced ownership checkpoint. Keep bootstrap/config
fingerprints with the caller's evidence; the runner checks `config.toml` and
the pinned executable/version before and after observation and the send/reply.

Every invocation requires the exact raw config SHA before any native process;
malformed or unreadable TOML refuses. Within that invocation only, native Codex
may increase the existing nonnegative integer at
`tui.model_availability_nux.<exact selected model>`. Unchanged bytes also pass.
All other parsed keys, values and types must remain identical. A missing/new
leaf, bool, decrement, trust/provider/sibling change, or formatting-only rewrite
refuses. A counter increase cannot hide another substantive change. This fixed
comparison does not change enrollment policy or authorize config writes.

The native identity JSON retains a `config_comparison` receipt with raw
before/after hashes, the exact allowed field, effective before/after identities
and comparison outcome. It contains no config values. A permitted in-run
rewrite still invalidates reuse of that raw tuple: the next invocation needs
a fresh exact pin. A failed earlier journey remains non-green; this exception
does not supply missing reply, convergence, idle or cleanup evidence.

The same invocation first runs native account/read, rate-limit owner read and
account reread, comparing owner IDs to the binding. It then drives catalog,
spawn, readiness, one marker prompt, a matching assistant reply, existing
convergence checks and owned generation-fenced close. The spawn's account
binding and effective model/effort must match before input. Preserve the raw
exit and JSON plus actual process identity, useful reply evidence, durable
closed session row and physical pane absence. A successful account read alone
does not qualify startup/send/reply.

`PASS` / exit 0 requires both native identity and the completed smoke cell.
Native probe failures return `UNTESTED` / exit 2 without spawning. Existing
quota/throttle/host-unavailable classifications remain non-green; missing
reply, identity mismatch and failed cleanup cannot pass. Stop after one
diagnosed environmental failure. Do not retry-spawn until capacity passes.

Evidence qualifies only the recorded candidate, host, account generation,
executable, config and model/effort. Input drift invalidates it even at an
unchanged repository SHA. Record other providers/hosts as UNTESTED and reuse a
result only under the existing exact-input gate-evidence contract. This runner
does not provision accounts, launch daemons, authorize resource windows or
certify an all-provider matrix. Without an explicitly authorized account window
and isolated candidate resources, retain that live acceptance prerequisite;
deterministic green cannot waive it.
