# Daemon structure contract

The daemon keeps optional work behind six named seams. Protected checks enforce
layer rules and compare each candidate exception inventory with the trusted base;
new exceptions are refused, while removing an existing exception is allowed.

| Layer | Contract | Rule owner |
| --- | --- | --- |
| F1 Protected chat core | Append, send claim, receipt and session lifecycle work gets priority over feature work. Bound feature reads, lock waits and retries; prepare off-writer and revalidate a short write. Background work registers with one capped scheduler. | Structure owner |
| F2 Event feed and projections | Rebuildable views declare their source of truth, incremental apply, full rebuild and parity check. Commit each projection cursor with the changes it applied. Presence and durable delivery outcomes remain outside this seam. | Structure owner |
| F3 Workflow policy | Optional workflow rules use one policy point and ship valid, proof-pending, legitimate-successor and true-refusal cases. Config switches may disable optional rules only. Authentication, generation fencing and consent remain enforced at the point of effect. | Structure owner |
| F4 Migrations and safe switch | Schema changes run as explicit timed steps before serving. Store open performs no history-sized work. Switching stops the serving writer before starting one writer; recovery requires independent custody, vacancy and compatibility proof. | Structure owner |
| F5 Observation | External facts include source, observation time and freshness; consumers read the value with its age. Observers register with the F1 scheduler. | Structure owner |
| F6 Durable delivery | Delivery records intent, attempt and landed, failed or uncertain outcome, with one recovery rule after restart. Routing clients use this layer instead of owning independent delivery state machines. | Structure owner |

Each installation stores its structure owner in the local, untracked file
`~/.config/pentacle/daemon-contract.json` as `{"structure_owner":"host:v2-seat"}`.
Keep the actual identity local. The public `contract.json` keeps
`structure_owner: null` and `structure_owner_status: "pending"`; the checker
validates that public state and reports only whether local configuration is
present. The configured maximum is two active daemon lanes. FD enforces lane
admission; the repository checker validates the shared cap and pending owner
state.

The six supplied exception inventories are copied into immutable `exceptions/`
baselines and pinned by `inventory_manifest.json` against commit
`f153ca8adce2a38e0c5092b2dc98ff5aec16e06e`. The matching files in `active/`
are the current rule inputs. Each rule may remove entries as the corresponding
code moves behind its layer. Adding or moving an active exception is a gate
failure. The manifests are data, not permissions to introduce new code.

B04's protected-core and projection checks run with
`python3 services/chat-stream-v2/tools/daemon_b04_gate.py --base <merge-base-sha>`.
They verify the frozen core-import census and the F1/F2 exception selectors,
then check candidate imports, writer and scheduler boundaries, and bulk-view
references. `main.py` is the composition exemption for core imports. SQLite
connection sites are checked against the frozen and merge-base sources. The merge
runner and predeploy workflow run this check alongside the existing contract
and policy/observation/delivery checks.

Merge and hosted checks run the B04 and layer selectors with `--report-only`.
Their output lists findings and a count for the independent reviewer, who
accepts or rejects the reported lines by name. Existing admission data is
retained for historical replay. Stop adding admission blocks.

### Historical admissions for reviewed core correctness fixes

An independently accepted core correctness fix may need an exact B04 admission
for a short, keyed Store write revalidated in its transaction, an account
runtime operation, a protected callback or a deadline-bounded loop. A writer
may neither scan nor await. Feature work and new scheduling sites are ineligible.

For a work loop, the item's final reviewer must confirm its explicit deadline
or budget and that it does no steady-state work. A steady-state loop may only
report state it already owns under a recorded driver ruling for the accepted
spec. Review the code reached by the admitted call at the ruled commit,
including changes to arguments of an existing call. The observation adds no
loop, scheduling primitive, cadence change, sleep, retry or wait for work or
external state. It may copy a fixed number of bounded fields under an identified
existing progress lock. Only bounded memory bookkeeping occurs under that lock.
Formatting, callbacks and logging occur outside it.

The observation's in-memory work is bounded independently of event throughput,
queue length and payload size. Its only output may be a bounded diagnostic
record through the existing log sink. The reviewer names the trigger,
deduplication key and aggregate emission bound for the loop. Additional
Store submissions, awaits, subprocesses, network or file operations, sinks,
retries, backlogs, feature work and policy work are ineligible.

Run the B04 checker and its admission generator with `python3.13`. The stored
signatures use that interpreter's AST representation, as in CI.
Use `python3.13 tools/emit_b04_admission.py --help` for the arguments to generate
a proposed block from the base and candidate commits. Each item in `b04_f1_admissions.json` carries its
spec ID, independent spec acceptance report ID, one-line reason and scopes.
A scope lists the repo-relative path, qualified symbol and exact scanner nodes
(kind, SHA-256 of the UTF-8 AST fingerprint, positive count above frozen).
Remove obsolete entries with their sites.

Each block's `sha256` covers its other fields encoded as UTF-8 JSON with sorted
object keys, compact separators, unescaped Unicode and no trailing newline.
The item's independent final reviewer must explicitly rule that block digest.
Record its digest and final report ID in the PR body and merge approval. When
stacking, verify the manifest is exactly the union of the ruled blocks and
recompute the whole-file byte digest in `ADMISSIONS_SHA256`.
