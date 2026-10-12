# Recover an assistant on another execution host

This command prepares and advances an explicitly commissioned replacement while retaining the daemon on its existing host. It uses existing spawn, rebind, consent, reparent, close, wake/watch, question and publication operations. It creates no permission, copies no provider session and restarts no daemon.

The initiating executor must authenticate independently of the predecessor. Provision its commission, target runtime, SSH access, private inputs and artifact before an outage. An exhausted or dead predecessor supplies no command or credential.

## Prepare the private inputs

Keep the manifest and journal outside this repository, owned by the executor and mode `0600` in a private directory. The target's script/manifest must already be staged. Use explicit paths and verify their source digest. Use existing token environment/files; never put tokens, device identifiers or checkpoint bodies in arguments or review receipts.

| Input | Purpose |
|---|---|
| `operation_id` | Stable identity for this recovery; do not change it to retry. |
| `endpoint`, `binding_name`, `composite_stream` | Exact retained daemon and assistant; set `AGENT_ORCH_WS_URL` to the same endpoint. |
| `predecessor`, `predecessor_generation` | Read from the current binding immediately before launch. |
| `authorization` | Explicit owner references for `spawn`, `rebind`, `retire`, `routes`, and optional `rollback`; prepare all needed references before advancing. |
| `spawn_spec_ids` | Owner-commissioned canonical recovery and configured rebind specs. A spec attachment alone grants nothing. |
| `target` | Explicit host/provider/model/effort plus staged `python`, `script`, `manifest`, `journal` paths. |
| `snapshot_command`, `snapshot_inputs` | Retained-host read-only adapter; inputs name sessions/notifications databases and the deployed authority-membership table explicitly; no private assistant name is a default. |
| `route_command`, `routes` | Retained-host selected-field adapter and effective consumer checks. |
| `mirror_command` | Authorized correlated-dispatch/canonical-final verification adapter. |
| checkpoint/bootstrap pointers | Private durable context, supplementary to current daemon state. |

The retained host can run `snapshot` and `route-files` as adapters: both read their JSON input from stdin. `snapshot` opens databases read-only and selects state, not tokens or consent keys. Capture its output into the private runtime journal, never into review logs. It includes current children, grants, subscriptions, active work, pending route references and question obligations. These facts come from the daemon, not a last hand-written checkpoint.

Every route names `id`, `path`, `format` (`json` or `plist`), `fields` (`keys`, `before`, `after`), `verify_command`, and `expected_readback`. Optional `activate_command` runs only when the effective readback is wrong. The adapter CAS-checks selected fields, preserves other content and verifies the loaded consumer. `$successor_stream` / `$successor_generation` substitute the actual successor. Store selected rollback fields/readbacks, not whole secret configurations. A configured local-only native wake requires an explicit fail-closed degradation with independent push/Updates readbacks; changing its stream id cannot make it a remote transport.

## Launch and serve before consent

```sh
python scripts/assistant_recovery.py plan --manifest /private/recovery.json --journal /private/bootstrap.json
python scripts/assistant_recovery.py start --manifest /private/recovery.json --journal /private/bootstrap.json
```

`plan` is read-only: no spawn, consent, binding, adoption, closure, registration, routing or publication. `start` uses one stable existing daemon-spawn request/idempotency key and an explicit top-level/default-visible target. The configured-spec rebind exception requires that topology and qualified provenance. The initiating executor retains its own authenticated wire identity without parenting the target; it never claims the predecessor's identity. A fixed-authority pending ruling retains the same spawn intent and defers readiness.

The successor reads its brief and runs:

```sh
python /private/assistant_recovery.py advance --manifest /private/recovery.json --journal /private/successor.json
```

It requests the exact generation/revision-bound designation early, self-rebinds, and reads the binding back. It can now answer authenticated composite dispatches. The one recovery operator action is the successor's Face ID designation. Blanket preauthorization is unsupported.

Current consent projection reaches operator clients, not the requester seat. Therefore run `advance` **once at the start of each ordinary dispatch/tell turn** until adoption completes. It reconciles `consent.status` once. No polling loop, timer, second manual wake or implied approval. Before designation, manager closes and cross-parent child adoption are unavailable. Existing children, old-owner subscriptions and hard-wired routes remain pending transfer. Independent fixed-authority spawn rulings remain separate; the new primary can request them, and denied/unavailable rulings retain their own fences.

## Adopt, retire and verify

After approval the next ordinary turn seals the transfer plan before reparenting cancels custom watches, adopts all children/advisors across hosts, and restores an open predecessor parent. Closed ancestry stays obsolete. Manager retirement requires the existing nonprotected, fresh-idle-or-proved-gone and no-pending-spawn fences. Busy, unknown, unreachable or changed inventories keep the successor answering with incomplete retirement. A commission does not override those fences.

Cold reconstruction includes lifecycle-retired rows and enqueued-but-undelivered wake facts. Explicit cancellation is not resurrected. Absolute future wake due times survive; uncertain execution becomes an obligation to reconcile, not an exactly-once business-action replay. A retained custom watch must match the watched child's generation before adoption and registration. The existing API has no atomic expected-generation argument: a mismatched registration receipt is cancelled and recovery remains incomplete.

Question obligations include fresh inventories before and after retirement, including questions created while retirement was deferred. They retain accepted answers or daemon-owned lifetime, or recreate still-required ordinary work after old-producer expiry. The exact recreation envelope is saved before submission and replayed unchanged after a lost response. Legacy generationless and generation-bound approval questions require explicit review/fresh requests.

Supported routes require effective readbacks. A failed consumer check is incomplete recovery. After a new correlated dispatch produces one canonical final:

```sh
python /private/assistant_recovery.py verify --manifest /private/recovery.json --journal /private/successor.json
```

Only `state=verified` proves all command stages and the mirror adapter passed. On transport uncertainty preserve the journal and retry the same operation. Foreign generations, revisions, ownership or field changes refuse; inspect the recorded stage rather than force through it.

Rollback before designation cancels the outstanding intent and can CAS-rebind an alive predecessor. After designation authority rollback needs new phone consent. A genuinely dead predecessor requires roll-forward. Do not treat an already-closed pane, build or PID as a working rollback target.

## Validation and limits

Run the focused socket/contract checks from a provisioned checkout:

```sh
python -m pytest services/chat-stream-v2/tests/test_assistant_recovery_command.py -q
```

Use an isolated daemon, databases, HOME, config and tmux sockets for a process drill. Physically kill the owned predecessor for the unavailable cases, verify it cannot act, and test both open-row and already-closed entry. Use real cross-host transport, signed stand-in consent, all child/wake/routing readbacks, singular canonical publication, retries and failure-path cleanup. Exercise real provider admission separately when a fixture transport is a scripted process; label that distinction. Retain source/environment/evidence hashes outside this public tree.

This tool does not prove a real phone ceremony, copying a provider session, an unprovisioned host, or recovery when the retained daemon host is down. Those are separate prerequisites or disaster-recovery work. QA and a dry-run never authorize changing a live assistant, grant, predecessor, external route or daemon.
