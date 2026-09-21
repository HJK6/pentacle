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

## Replay and satellite acknowledgement

`request_id` is correlation only. The durable source record key is the
idempotency key, so replaying the same source records with either the same or a
new request ID produces zero new writes and reports `usage_replayed`. The
satellite keeps its sanitized usage span pending and advances its source
high-water offset only after the coordinator returns the usage acknowledgement.
Rejected usage leaves that stream pending and its offset unmoved.

## Candidate evidence

The candidate-bound manifest is `_artifacts/usage/stage1/manifest.json` and is
validated with:

```sh
python3 services/chat-stream-v2/tools/usage_manifest.py validate \
  --manifest _artifacts/usage/stage1/manifest.json
```

The manifest records the candidate/base SHA, exact focused selector, overlay
digests, coordinator and satellite PID/checkout readbacks, deferred Nexus pin
status, stream generations/source digests/replay counts, and RED/focused/JUnit
and prechange receipt hashes. Nexus remains the only owner of the global
`event_push.target_sha` stage, activation and rollback window.
