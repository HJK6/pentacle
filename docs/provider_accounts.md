# Named provider accounts

Named accounts are optional and **default OFF**. They let concurrent agents on
one host use independent subscription logins. The front desk chooses an account
for a launch or changes the saved host/provider default. A default change affects
future launches; each running agent keeps its original home and login generation.

## Controls

Use the existing authenticated front-desk seat. A role name or claimed stream ID
alone grants no account-control authority. Its authenticated successor inherits
control through the existing assistant binding. Workers can spawn through their
existing grants using the default, and read account status; they cannot choose
an account override or change a default.

```sh
agent-orch account status --host host-a --provider codex
agent-orch account default set --host host-a --provider codex --account primary
agent-orch spawn --host host-a --provider codex --initial-prompt 'Continue the assigned work'
agent-orch spawn --host host-a --provider codex --account spare --initial-prompt 'Continue the assigned work'
agent-orch usage --account primary --json
```

An explicit account changes only that launch. Unknown, disabled, unavailable,
wrong-provider and unprovisioned selections fail clearly, with no alternate
account, default rewrite, model/provider change or hidden login attempt. Explicit
account choices currently apply to immediate spawns. Ordinary scheduled spawns
resolve the saved default when they execute.

Priorities, exclusions, caps and ratios remain front-desk instructions. For
example, the front desk can explicitly launch three agents on one permitted
account and two on another. There is no ratio scheduler, fixed account priority,
percentage reservation ledger or automatic quota/auth migration.

## Revalidate an identity-conflict flag

A successful ordinary probe does not clear a previous identity conflict. The
authenticated front desk can explicitly revalidate an already enrolled, active
generation after checking its status. Copy its exact generation and
`health_revision` from that readback; do not guess the revision.

```sh
agent-orch account status --host host-a --provider claude --account primary
agent-orch account revalidate-health --host host-a --provider claude --account primary \
  --login-generation login-one --expected-revision 7 \
  --operation-id desk-revalidation-one --reason identity_reviewed
```

This control requires enabled account policy and a managed host. It performs one
bound native observation with the existing identity and freshness checks. Only
an unchanged identity-conflict row can become healthy; a newer failure, revision,
generation or binding change refuses the transition. Authentication must be
verified again for every request. No login, account selection, default change or
session migration occurs. The old failure remains visible as history.

The audit receipt and health transition commit together. Reusing an operation ID
with the same arguments reads its original receipt without another native call;
changed arguments refuse. A replay is historical: check current status before
admission, since a later failure may have changed health again. Interrupted
requests never heal or automatically retry. On daemon restart their pending
receipts become interrupted, including while policy is off, without changing
health or defaults. A new explicitly chosen operation ID can proceed through
the existing host locks. Store connections alone do not interrupt requests.

The default RPC deadline is 210 seconds, as for a probe; explicit shorter
deadlines are honored. Host queue time can exceed a caller's deadline. Read the
same operation's receipt/status instead of blindly repeating provider work.

## Move a conversation

Prepare the source conversation's checkpoint, including its objective, current
work, evidence and outstanding actions. The front desk supplies it through the
existing handoff prompt interface; the operator need not repeat the conversation.

```sh
agent-orch spawn --handoff --handoff-from host-a:conversation --host host-b \
  --account spare --initial-prompt-file /absolute/checkpoint.md
```

For the current seat, omit `--handoff-from`. This is the ordinary handoff path:
it carries context through the initial prompt and transfers existing custody,
using the existing successor-delivery, busy/uncertain-action and retirement
protections. A managed handoff without a nonempty checkpoint/prompt refuses before
launch. The predecessor's binding remains immutable; the successor gets a fresh
native session in its selected home. No native cross-account resume, transcript
copy or credential copy is performed. A pending or failed handoff is reported
through the existing receipt; its source is preserved when delivery is unproved.

## Private configuration and enrollment

`PENTACLE_PROVIDER_ACCOUNT_POLICY` optionally names a mode-0600, owner-controlled
JSON file. With no file, the feature is off. A minimal disabled policy is:

```json
{
  "enabled": false,
  "managed_hosts": ["host-a", "host-b"],
  "cells": [
    {"host": "host-a", "provider": "codex", "alias": "primary"},
    {"host": "host-a", "provider": "codex", "alias": "spare"}
  ],
  "bindings": [],
  "executors": {},
  "watchers": {},
  "account_caps": {}
}
```

`managed_hosts` allows a staged rollout. An untouched host keeps its existing
launch path. While policy is enabled, removing a previously managed host from
that list refuses new launches there. Disabling the whole policy restores the
legacy path for ordinary new launches even when account rows remain; explicit
account choices and resumes of bound named-home conversations still refuse.
Missing entries in `cells` are not
invented accounts; configured but unenrolled entries appear as unconfigured.
Defaults live in the daemon's transactional store, survive restart and carry a
revision. Selection reads a complete account/default revision once before launch.

To manage only selected providers on a host, add an explicit provider scope:

```json
{
  "enabled": true,
  "managed_hosts": ["host-a"],
  "managed_providers": {"host-a": ["codex"]}
}
```

This is a scope fragment; enrollment receipts, cells and executors still need
their normal configuration. Omitting `managed_providers` keeps the existing
host-wide behavior. When present, its keys must exactly match an explicit
`managed_hosts` list, and each value must be a list containing only `codex`
and/or `claude`, without duplicates. Empty lists are allowed. Invalid scope
configuration refuses startup rather than guessing which provider is managed.

An ordinary new launch of a never-managed provider outside scope keeps its
legacy path. Explicit account choices and bound named-home resumes outside
scope refuse. Removing a previously managed host/provider also refuses new
legacy launches there while its retained account bindings exist. Missing cells,
missing defaults and failed enrollment never select a legacy or alternate
account. Existing bound seats keep their homes and attribution. Account
mutations, automatic probes, health revalidation and startup receipt imports
respect the same provider scope; read-only status preserves historical rows.
Raw command launches still refuse whenever any provider on the target host is
managed. Provider-less admissions also refuse while retained account rows exist.

Each enrolled host has a private mode-0700 directory containing its mode-0600
`~/.config/pentacle/provider-account-host.json`. Its configuration contains:

- `host`, absolute `registry` and managed `root` paths; both outside repositories,
  shared memory and synchronization roots.
- `cells`, keyed by `provider:alias`, with `enabled`, secure `profile_ref`, pinned
  `executable`, exact `version`, `executable_sha256`, `platform` (`macos` or `wsl`),
  `model`, `effort`, trusted absolute `cwd`, and an allowlisted non-secret
  `bootstrap` mapping. A wrapper can also specify the native `observer_executable`
  and its SHA256; launch keeps the wrapper while transcript proof uses the native
  executable. The launch preserves the wrapper's non-secret secure-store/keychain
  configuration and pins its native executable from the binding; credential
  values and inherited native-executable overrides are excluded.
- The intended `expected_provider_id`, or an `expected_email` from secure account
  records for the first login. Native status must verify that identity before
  the returned UUID is pinned and before the minimal subscription turn. Email is
  checked in memory and omitted from readbacks. If both expectations are supplied,
  both must match.
- `browser_helper`, with the installed shared `account_browser.py` path and its
  SHA256, and a configured `claude_usage_command` using the existing usage adapter.
  Existing secure browser/profile and Keychain setup remains host-owned.

The daemon's `executors` map selects each host's reviewed Python and host-helper
script through the existing authenticated host transport. `bindings` imports
verified non-secret host enrollment receipts. Profile references stay host-local.
A stale startup receipt never reactivates an older generation.

Enrollment is an explicit rollout step, not a startup side effect. Use the host
helper on the selected host only under the deployment owner's live grant:

```sh
python /absolute/provider_account_host.py enroll --provider codex --account primary \
  --login-generation unenrolled --operation-id enrollment-one --new-generation login-one
```

The helper resolves the home and browser profile from private configuration. It
performs native login in the final absolute generation home, validates subscription
identity/storage, makes a minimal turn with the configured model/effort and
activates atomically. Codex uses explicit `CODEX_HOME` with file credentials;
Claude uses explicit `CLAUDE_CONFIG_DIR`, directory-specific native Keychain on
macOS and file storage on WSL. OS `HOME` remains unchanged. New homes are 0700;
registry and credential files are 0600. Auth-environment overrides are scrubbed;
paid API/Console authentication is rejected.

Never copy refresh tokens or rename a Keychain-bound home. Only the allowlisted
non-secret bootstrap is provisioned. A wrong, failed, cancelled or locked-store
login leaves existing bindings and peer accounts intact. A busy browser profile
is refused; the enrollment attempt closes only its own browser session. Missing
profiles or human authentication needs return a safe code. The front desk drives
the flow to that step and supplies the specific host/account browser instruction.
Authorization URLs/codes and credential contents are not logs or evidence.

## Usage, availability and re-login

Usage reads the bound home, joins native identity coherently and reports every
applicable window. Accounts on different hosts sharing the same verified pool
produce one pool readback using the latest observation; percentages are not summed.
The readback includes timestamps, freshness, reset, original identity, conflict
and configured cap. Missing/stale/incomplete usage is visibly unknown, never zero.
Freshness is informational; there is no new fixed-age launch admission gate.

Claude's native cache writer throttles updates for 60 seconds. Bound observations
wait, when needed, for cache-write eligibility under the existing host slot,
with a 61-second monotonic limit and one native usage collection. Freshness is
measured against the observing host's own start time; another native writer's
same-identity fetch during that operation is valid. A stale, missing or malformed
cache returns `provider_cache_stale` and a failed collection, preserving account
health. Actual identity disagreement still refuses and marks the account unsafe.
Neither result clears an existing authentication or identity failure.

The observe host alarm is 180 seconds, its daemon command deadline 200 seconds,
and the `account probe` CLI default 210 seconds. Other account actions keep their
prior deadlines. An explicit shorter timeout is honored. Time queued behind
another cell can exceed the caller's deadline; that is a caller timeout, not an
identity failure or permission to retry login. The daemon operation remains
bounded. Enrollment retains its prior budget and immediate identity readbacks;
a throttled second enrollment readback can still refuse safely. Existing falsely
marked health rows require a separately authorized disposition, not automatic
healing from a successful usage probe.

`account_caps[provider][verified_pool_id]` carries the existing operational cap
value verbatim for that account across hosts. It does not introduce new units,
percentages, margins or enforcement semantics. Do not infer an account allocation
from an old host-named record or invent headroom; the front desk resolves the
mapping through its existing policy source before relying on it.

Transcript-derived usage retains the original per-stream generation binding and
native identity separately. Reopening a stream or changing a default cannot
rewrite historical attribution. Legacy unknown identity remains unknown, and a
native identity disagreement remains visible.

An authenticated bound probe or watcher auth failure marks only the affected
host/provider/account generation unavailable and records a typed fact through
the existing error-alert path. Alert delivery follows the existing configured
alert policy. Other independently enrolled hosts and accounts remain usable.
Quota, capacity and transport failures do not trigger login or an alternate spawn.
A successful usage read alone cannot clear an unavailable login.

The front desk can request targeted repair when ready:

```sh
agent-orch account probe --host host-a --provider codex --account primary --login-generation active
agent-orch account relogin --host host-a --provider codex --account primary \
  --login-generation active --operation-id repair-one --timeout 330
```

Repair verifies actual auth failure, uses same-cell/host serialization, the
existing 900-second cooldown and three-attempt hourly limit, and a fresh final
home. An uncertain reply is inspected using the same operation/generation instead
of starting another login. Wrong-account, human-step and failed outcomes are safe
metadata; no healthy peer account is logged out or moved.

The shared `recover-codex`/`recover-claude --account ALIAS` wrappers submit bound
requests to this same daemon path. They do not run another login loop. Managed
`credwatch` uses an explicit provider/account/generation matrix and a fixed
host-watcher principal with a private token file. Its mode is
`CREDWATCH_MANAGED_ACCOUNTS=1`; `CREDWATCH_MANAGED_CELLS` supplies that matrix.
The policy's `watchers` maps `host:account-watcher` to `host` and `token_file`.
Only that authenticated host watcher can submit trusted observations/failures;
ordinary workers may report only their own launch cell. Pending requests keep
stable IDs through daemon outages. Provider-global legacy recovery refuses while
managed mode is active.

## Release and rollback

Merge source with the feature off. Runtime repins, restarts and enrollment are
separate deployment-owner grants; a source merge is not activation. Use the applicable repository gates and independent final-candidate QA, preserve
non-secret config/code preimages, coordinate shared resources and prove the
actual deployed journey under the deployment owner's grant.

Enable only compatible launch, local/satellite ingest, usage and recovery consumers
at their accepted artifacts. Bind real evidence to source/config/binary hashes,
login generation and live process identity. Preserve healthy legacy homes.

Enable policy and configure each managed host/provider's saved default in the
same activation window; verify an ordinary spawn with no account override.
An enabled host without a saved default refuses ordinary spawns.

Rollback disables new controls/new managed launches first and restores ordinary
legacy admission, while existing bound agents can drain. Retain binding and
default rows; no database restore or account-row deletion is needed. Use the
existing handoff before reverting to a binary that
cannot understand named transcript roots. Keep saved defaults and immutable
bindings coherent. Never restore stale token bytes, wipe homes or log out another
account as rollback. Missing host/provider/account enrollment is incomplete and
must be reported explicitly.
