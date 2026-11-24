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

One configured structure-owner identity signs daemon structure decisions. The
identity is intentionally pending until the operator returns the owner card.
The configured maximum is two active daemon lanes. FD enforces lane admission;
the repository checker validates the shared configured cap and owner setting.

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
references. `main.py` is the composition exemption for core imports. The F4
SQLite connection exemption is limited to the accepted offline preparation
sites in the B04 parent; new connections still require admission. The merge
runner and predeploy workflow run this check alongside the existing contract
and policy/observation/delivery checks.
