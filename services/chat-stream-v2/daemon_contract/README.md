# Daemon structure review

Review changes against the layer that owns the behavior.

| Layer | What to check |
| --- | --- |
| F1 Protected chat core | Append, send claim, receipt and session lifecycle work gets priority over feature work. Bound feature reads, lock waits and retries. Prepare off-writer and revalidate a short write. Background work registers with one capped scheduler. |
| F2 Event feed and projections | Rebuildable views declare their source of truth, incremental apply, full rebuild and parity check. Commit each projection cursor with the changes it applied. Presence and durable delivery outcomes remain outside this seam. |
| F3 Workflow policy | Optional workflow rules use one policy point and ship valid, proof-pending, legitimate-successor and true-refusal cases. Config switches may disable optional rules only. Authentication, generation fencing and consent remain enforced at the point of effect. |
| F4 Migrations and safe switch | Schema changes run as explicit timed steps before serving. Store open performs no history-sized work. Switching stops the serving writer before starting one writer. Recovery requires independent custody, vacancy and compatibility proof. |
| F5 Observation | External facts include source, observation time and freshness. Consumers read the value with its age. Observers register with the F1 scheduler. |
| F6 Durable delivery | Delivery records intent, attempt and landed, failed or uncertain outcome, with one recovery rule after restart. Routing clients use this layer instead of owning independent delivery state machines. |
