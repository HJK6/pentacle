# Stage 1 usage accounting

Stage 1 accounts for native provider observations arriving from the existing
authenticated satellite `event.push` path. It does not add a daily line,
budget alert, scheduler, pricing, fleet total, service or table; those belong
to the separate Stage 2 work item.

## Admission and host binding

The bearer `push_secret` is checked first. A usage-bearing push then carries a
lowercase HMAC-SHA256 proof over the exact UTF-8 message

```text
event.push.v1\0<host-claim>\0<satellite-sha>\0<satellite-pid>
```

The coordinator resolves `event_push.host_secret.<host-claim>` from its
existing key/value store (with the configured host-secret fallback), verifies
the proof constant-time, and derives the authenticated source host from that
verified identity. A caller cannot provide `collection_host`; the existing
`record_usage` host-equality predicate remains the final write fence.

Each usage item is deliberately small and exact: stream ID, provider,
coordinator-issued session generation, source pane PID, a redacted native
session ID, a SHA-256 source-file identity digest, and provider-native token
records. Paths, prompts, credentials and secrets are rejected. Source digest,
generation, provider, pane PID and open-row checks happen before a usage write.
The first admitted digest is atomically stored in the existing
`observer_binding.transcript` object; a later mismatch is rejected.

## Native and derived fields

The existing `v2_usage_state` and `v2_usage_records` tables remain the only
accounting storage. Codex keeps native `input_total`, `cached_input`, `output`
and `reasoning` unchanged. When the first two are valid numbers, snapshots add
the explicitly labelled projection:

```json
{
  "derived_tokens": {"uncached_input": 90},
  "derived_fields": {"uncached_input": "input_total - cached_input"}
}
```

Cached input is not added a second time. Claude's native `uncached_input`,
`cache_read`, `cache_write` and `output` fields remain native values.

## Codex ledger row versus response rows

The cumulative Codex ledger row is the per-field maximum of the rollout's `token_count.info.total_token_usage`.
That client counter is not the sum of the session's responses: it never applies a response whose `token_count`
carries `last_token_usage` of zero (total unchanged), nor a final response written without a following
`token_count`, and the daemon may stop collecting before the last snapshot. Each `token_usage_record` also
carries `thread_token_usage`, the thread's own counter after that response, which equals the running sum of the
rollout's own response `usage` records. On the 2026-10-07 read-only sample reconciliation
(`tools/usage_codex_sample_reconcile.py`; finding in
`spec_pentacle__usage_codex_ledger_vs_rows_sample_reconciliation_2026_10` § Readback) 1,289 of the 1,314
`unverifiable` sessions had a complete, consistent row set whose sum equals the final `thread_token_usage` while
the ledger row was lower; the other 25 had response rows missing from the head of the session (collection began
after them), so their rows are incomplete. Nothing here changes classification: `unverifiable` stays the label
until a separate amendment stores `thread_token_usage` with the rows. Buckets: cached input is inside input
and reasoning is inside output in every checked session.

The tool is read-only (`mode=ro`, no write path), reads rollouts on the host that holds them, and hashes native
and account ids in its output:

    python3 tools/usage_codex_sample_reconcile.py transcripts --host <host> --ids-file ids.json --hash
    python3 tools/usage_codex_sample_reconcile.py sample --ledger <ledger copy> --seed <n>
    python3 tools/usage_codex_sample_reconcile.py join --ledger <ledger copy> --facts facts.jsonl --out table.csv

## Replay and satellite acknowledgement

`request_id` is correlation only. The durable source record key is the
idempotency key, so replaying the same source records with either the same or a
new request ID produces zero new writes and reports `usage_replayed`. The
satellite keeps its sanitized usage span pending and advances its source
high-water offset only after the coordinator returns the usage acknowledgement.
Rejected usage leaves that stream pending and its offset unmoved.

## Candidate evidence

The candidate-bound manifest is `_artifacts/usage/stage1/manifest.json` and is
validated as schema version 2 with an independently supplied deployment contract:

```sh
python3 services/chat-stream-v2/tools/usage_manifest.py validate \
  --manifest /private/evidence/usage/manifest.json \
  --deployment-contract /private/config/usage-deployment.json
```

The manifest records the candidate/base SHA, exact focused selector, overlay
digests, coordinator and satellite PID/checkout readbacks, deferred Nexus pin
status, stream generations/source digests/replay counts, and RED/focused/JUnit
and prechange receipt hashes. Nexus remains the only owner of the global
`event_push.target_sha` stage, activation and rollback window.

The contract has schema `pentacle.usage-deployment`, version 1, and exact
`coordinator_host`, `satellite_hosts`, `pin_owner` and `prechange_hosts` fields.
Copy `configs/usage_deployment.example.json` outside the source checkout and
set them for the authorized deployment before gathering the candidate manifest.
An empty satellite array explicitly supports a single host. The nonempty
prechange host list names the required historical readbacks; those receipts may
include an authority host outside the usage runtime inventory. Missing contract,
unknown/duplicate hosts, changed owner or missing/extra runtime/prechange hosts
fail validation. Expected identities come from this trusted input, never from
the candidate manifest itself.

Version 2 changes `runtime.satellites` to a generic host map and groups historical
receipts under `receipts.prechange[host]`, alongside `red`, `focused` and `junit`.
Version 1 is refused; migrate all three deployment assumptions together. Runtime
PID/SHA, exact candidate/overlay digests, authenticated stream-host inventory,
generation/replay and receipt-file digests remain checked. The JSON validation
receipt binds the exact deployment-contract SHA256. `--no-file-check` reports
`file_checks: false` and is structural evidence only.

To include this validation in the daemon merge gate, supply both
`--usage-manifest` and `--usage-deployment-contract`; neither is needed for an
ordinary daemon merge gate. Keep real contracts, manifests and receipts private.
