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

### Explicit bound-executable upgrade

A healthy generation can be independently enrolled with an alongside, pinned
native executable and then imported with `agent-orch account upgrade`. Ordinary
`relogin` still requires a native authentication failure. Upgrade never starts
login, retries authorization, changes the saved default or rewrites existing
session bindings. The current authenticated front desk must own the operation.

The deployment owner freezes admission for the exact host before enrollment,
records the hold's reason/set_at/held_until tuple and retains all generations and
journals. Keep the native vendor subtree intact, pin its native hash/version in
the host cell and preserve the predecessor executable/home. Before login, the
operator must be ready in a browser with a tested localhost callback route.

The reviewed receiver checkout supplies the one private URL relay:

```sh
python services/chat-stream-v2/tools/provider_account_operator_login.py \
  --policy /absolute/private-policy.json --host host-a --provider codex --account primary \
  --login-generation login-one --new-generation login-two --operation-id upgrade-one
```

This command verifies the live assistant binding and calling seat token, reads
the host executor paths from the owner-controlled policy, and invokes the host
helper over configured SSH. The policy may be OFF during enrollment. It emits
exactly one validated HTTPS authorization URL to the authenticated binding's
private composite, then creates a separate Done / Not yet action prompt. Only
publication/prompt IDs and the safe native outcome reach stdout. Done is not
login proof: native completion, subscription identity and the minimal turn must
all succeed. No automated browser broker is used in this mode.

The host CLI option `--operator-browser-fd 3` is Codex-enroll-only. It accepts an
inherited open write pipe/socket >=3, closes it after one bounded URL, and makes
it non-inheritable by native children. Do not redirect it to a file, terminal,
stdout or stderr. The relay owns its SSH child and has a 320-second deadline,
beyond the host's 300-second enrollment alarm. Never log URLs, codes or tokens.

On success, enable the managed Codex policy while retaining the same live hold:

```sh
agent-orch account upgrade --host host-a --provider codex --account primary \
  --login-generation login-one --new-generation login-two --operation-id upgrade-one \
  --expected-version 'codex-cli PINNED_VERSION' --expected-executable-sha256 "$NATIVE_SHA256" \
  --reason approved_bound_executable_upgrade --timeout 210
```

The receiver hashes the operation ID exactly as the relay does for host journal
lookup. It reads only the completed enrollment receipt and observes the new
active host generation. Provider/subscription/pool, cell, home, native pin and
coherent usage must agree. It compares the old active generation/health revision,
saved default and unexpired unchanged hold immediately before atomically writing
the new binding, observation and successful receipt. It refuses workers,
watchers, OFF policy, unmanaged providers, stale generations and split evidence.
The default deadline is 210 seconds.

Use the same arguments and operation ID to inspect an uncertain import. Pending
retry repeats only status/observe; startup never completes it. A completed replay
still requires FD authentication and enabled managed policy, but no surviving
hold or old active generation. It returns historical evidence, not current
headroom. Changed arguments under the same ID refuse. A failed import leaves the
old receiver selection intact even if the host has activated the new generation.

Before releasing only the unchanged owned hold, prove host/receiver agreement,
unchanged default and immutable existing-session attribution. A deployment owner
must separately authorize installation, helper/CLI rollout, receiver activation
and actual launch qualification. Source tests are not activation proof.

Rollback is OFF safe parking. Before host activation, restore only non-secret
config/policy preimages. After activation, keep admissions frozen and disable
policy; verify legacy admission and explicit named/bound-resume refusal before
releasing the owned hold. Preserve all generations/journals for a supported
same-operation import or separately authorized fresh login. Never restore token
bytes, a database snapshot or an old registry pointer. Existing bound seats may
drain with immutable attribution.

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

A Codex cell may instead name `adopt_home`, an absolute private (0700, owned,
non-symlink) home that is already signed in to that account. The `adopt` action
takes the same arguments as `enroll` and registers that home in place. It runs no
login, never creates, copies, moves or renames credential files, and writes only
absent bootstrap files. Activation still needs the pinned binary, the expected
subscription identity, a minimal turn and an unchanged identity reread. The
registry accepts only the configured adopted homes outside the generation layout.
Retained `config.toml` must pass the same key allowlist and file-store policy as
bootstrap configuration; adoption refuses invalid contents before native execution
and preserves accepted files. Each home belongs to one `provider:alias` cell;
duplicate configuration and reassignment of a registered home are refused, including
after a host executor restart. The same cell may adopt a new generation in place.
The minimal turn must return exactly `OK` after surrounding whitespace is stripped.

## Usage, availability and re-login

Usage reads the bound home, joins native identity coherently and reports every
applicable window. Accounts on different hosts sharing the same verified pool
produce one pool readback using the latest observation; percentages are not summed.
The readback includes timestamps, freshness, reset, original identity, conflict
and configured cap. Missing/stale/incomplete usage is visibly unknown, never zero.
Freshness is informational; there is no new fixed-age launch admission gate.

Codex may emit an `account/updated` status before or after its first
`account/read` reply. Only an exact `authMode`/`planType` status for ChatGPT is
accepted, and its plan must match the first authoritative account reply.
Announcements never verify identity. Identity-bearing, malformed or conflicting
notifications refuse the observation with `provider_identity_changed`. The
account reads before and after quota collection must agree, and the quota
provider and pool identity must match the bound account. A successful ordinary
observation never clears sticky account health.

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

Disabling account policy parks new launches for every retained enrolled
host/provider. Raw commands are refused on enrolled hosts. Management readback,
reports and owner-directed draining remain available; new work to an old release
is fenced by the active exact pair. Retain binding and
default rows; no database restore or account-row deletion is needed. Use the
existing handoff before reverting to a binary that
cannot understand named transcript roots. Keep saved defaults and immutable
bindings coherent. Never restore stale token bytes, wipe homes or log out another
account as rollback. Missing host/provider/account enrollment is incomplete and
must be reported explicitly.


## One exact fleet release pair

`provider_cli.release_pin` is the daemon-owned desired state for both providers.
Its schema-1 JSON carries a positive monotonic revision and `providers`, whose
`claude` and `codex` entries have exact semver `version` and an `artifacts` map.
Artifact keys are `darwin-arm64`, `darwin-x64`, `linux-arm64` or `linux-x64`;
both providers must cover the same reviewed platform set. Each entry supplies
HTTPS release `provenance`, `native_sha256` and `resource_sha256`. No host or
account selects a different desired release. Paths stay in host configuration.

A deployment owner prepares the reviewed complete snapshot outside source:

```sh
agent-orch account pin-status
agent-orch account pin-set --pin-file /absolute/reviewed-pair.json \
  --expected-revision 0 --operation-id initial-pair --reason reviewed-release
```

First initialization uses absent revision 0; the snapshot supplies revision 1.
Every later move, including rollback, supplies the next revision and both
providers. An unchanged provider retains its artifact map. The receiver commits
the pair, prior record and immutable receipt atomically. Identical operation-ID
replay returns its historical receipt; changed payload or stale CAS refuses.
A missing or corrupt pin parks managed admission, independently of account-policy
OFF. Minimum-only installer manifests never attest exact managed admission.

Deploy compatible enforcing consumers on every participating host before initial
activation. In the named fleet window, stage official immutable artifacts beside
retained releases; verify their complete hashes, checkpoint/drain affected seats,
and acquire the existing authenticated operator/local-admin host admission hold.
Keep `PENTACLE_SEAT_OPERATOR_AUTHORITY` OFF. Source publication alone grants no
hold, installation, pin move, native proof or restart authority.

Host cell configuration names the staged `executable`, its native `version` and
`executable_sha256`, and `resource_root` for the complete immutable package. A
standalone file's resource digest is its byte SHA256. A directory's digest is
SHA256 of compact sorted-key JSON containing sorted `[relative_path, file_hash]`
pairs for every regular file. Symlinks, writable artifacts and unsupported
platforms refuse. Optional `resource_roots` maps retained executable paths to
their original complete resource roots, so verification never substitutes a
new package's resources for an older binding.

```sh
agent-orch account repin --host host-a --provider claude --account primary \
  --login-generation current-generation --new-generation release-successor \
  --operation-id reviewed-repin --expected-pin-revision 2 \
  --expected-pin-digest EXACT_REVIEWED_PAIR_SHA256 --reason reviewed-release
```

Repin derives its successor from the healthy current binding. It keeps the exact
home, transcript root, profile, provider/pool identity and saved default. It proves
native bytes/resources, subscription identity and one minimal turn, then reads
identity and artifacts again. It never logs in, copies credentials or changes a
home's authentication storage. The host lock and journal serialize proof and
activation. Same-user native processes sharing the provider executable are
ambiguous occupancy and must be drained; process environments are never read.
The central import compares the pin, health, default and exact hold tuple. A host
success with failed central import remains parked: reconcile that same operation
under a valid hold, without repeating its native turn. Auth, network, quota and
identity failures stay distinct; they never initiate login automatically.

If the pin advanced after host success, retry the old operation to retain its
exact journal evidence, then use a fresh `account repin` operation with
`--reconcile-of PRIOR_OPERATION_ID`, the still-selected central generation, an
unused prospective generation and the current pin revision/digest. This requires
a real, unexpired admission hold and unchanged original central binding, health,
default and policy. The authenticated host resolves and validates every repin
link, immutable registry row and original private home anchor. The affected cell
stays fenced across hold expiry and pin rollback until reconciliation commits.

When the active host artifact already matches the current pin, reconciliation
reverifies bytes/resources/version read-only and imports that generation; it
returns `repinned` and creates no prospective generation or additional native
turn. Otherwise it repins once from the validated host selection to the current
target. Only the final binding is selected centrally, atomically with every
missing lineage link. Original receipts and proofs retain their old revisions.
Retained successful identity/minimal-turn evidence has no age limit for ancestry;
current artifact verification is stored separately, and ordinary observation
freshness is unchanged. Same-pin, same-ID retry still reconciles normally.
A second race or uncertain/failed outcome parks the chain; it never triggers
login, historical selection or background retries. A fresh attempt must reference
the latest parked chain tip; a predecessor can be consumed only once. Definitive
host failure without a split remains for the existing FD recovery disposition.

Successful immutable repin receipts link old and new binding keys. Resume,
including automatic front-desk restore, follows only that proven same-home chain
for the same host/provider/account. Fresh-login/enroll/adopt transitions do not
supply repin lineage. Historical lifecycle attribution remains unchanged. CLI
version rollback creates another verified successor pointing to retained bytes;
it never reactivates a historical generation. If the current home is incompatible
with the retained native client, park it for separately authorized recovery.

Every new process rechecks the pair after asynchronous host verification and
records its admitted revision/digest/artifact per lifecycle. A process created
from an older committed admission after a pin move remains an enumerable old
seat. New normal work input to mismatching seats is refused while queued work
and receipts are preserved. Status/read, checkpoints, reports and closure remain
available. Same-release artifacts need verification rather than an unnecessary
restart.

An offline host remains pending and supplies no completion proof. On return,
current satellite/helper code admission precedes native verification; catch up
directly to the current pair, not missed intermediate releases. Disconnect during
an operation requires exact journal reconciliation before another operation.
`pin-status` reports each retained active cell's expected/observed release,
reachability, proof freshness and old seats; parked/offline cells prevent fleet
completion. Proof freshness expires after 120 seconds and never triggers an
installation, login or pin move.

For a dead front desk, only the server-authenticated operator or verified local
administrator may recover a pin. `pin-status` still returns the safe raw-record
digest and retained receipt IDs for a corrupt primary. Select an approved receipt
matching the front desk's retained healthy binding, then use:

```sh
agent-orch account pin-set --recover-receipt APPROVED_OPERATION_ID \
  --expected-raw-digest RAW_SHA256_OR_absent --expected-high-water-revision 4 \
  --operation-id recover-pair --reason restore-approved-pair \
  --local-admin-token-file /absolute/private/admin-token
```

Recovery loads only the receipt's exact pair and writes a revision above all
retained commits. It never trusts a revision parsed from corrupt bytes, accepts
arbitrary replacement JSON, authorizes repin or bypasses normal pinned restore.
Reverting the desired pair can fence cells already on a newer pair; inspect the
named cells before resuming. Lost receipt history is a storage-recovery blocker.
