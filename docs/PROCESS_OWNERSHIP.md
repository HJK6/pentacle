# Process ownership observations

`ps lstart` and Darwin's printed `kern.boottime` are presentation tokens. A
persisted token keeps its original format and rendering. Producers do not pin
or rewrite historical values. Comparison observations may use exactly UTC/C
and host-local/C with `TZ` absent, on the process's host; empty `TZ` is not a
substitute for host-local time on Darwin.

A positive supported birth rendering establishes conservative liveness. An
unmatched live PID is `process_identity_incomparable` for restore and direct
input replay. No observer binding establishes that a different birth is a
replacement: unknown observations never prove death. Manual restore uses the
same proof. Recovery for an indefinitely incomparable holder is an explicitly
commissioned successor spawn/rebind under existing authority, retaining and
explicitly disposing ambiguous input receipts.

Destructive comparisons retain boot, uid, command, lineage and pane fences.
Darwin boot comparisons use numeric sec/usec, ignoring trailing ctime text.
An alternate birth rendering additionally requires a source-qualified local
Sessions capture, matching proof/row/inventory host, stable numeric Darwin boot
across the observation, and a fresh UTC birth strictly before UTC `captured_at`.
Missing/ambiguous capture time, equal-second birth, clock drift and cross-host
provenance defer. Linux boot UUID equality is not clock provenance: its alternate
branch defers. Existing exact-rendering authority remains; its wall-clock-step
collision exposure and non-atomic timestamp/signal races are not eliminated.

## Consumer dispositions

| Producer → persistence/transport → consumer | Decision and disposition | Validation |
| --- | --- | --- |
| SpawnCtl local/remote `_pane_started_at` → observer binding → restore | Producer unchanged. Comparison observations use UTC and host-local. Only definitive process absence permits eligible restoration. | `test_process_ownership_semantics`, `test_assistant_restore` |
| Same binding → `AssistantComposite` direct-input death proof | Same restore contract; incomparable identity cannot create recovery replay authority. Existing input identity keeps retries duplicate. | `test_process_ownership_semantics`, `test_restart_input_residuals`, `test_restart_decision_fences` |
| `Sessions._capture_tree_identity` / `identity_proof` → `session_reap` → reconciler | Only the local Sessions close cohort (including CapturedTree readback) supplies production complete process proofs to this ledger. Remote close/presence readbacks do not establish this local capture provenance. Alternate matches require all additional clock fences; exact matches keep existing authority. Producers and proof shape unchanged. | `test_process_reap_semantics`, `test_session_reconciler`, `test_start_presentation_consumers` |
| `process_records` / Darwin kernel SID fallback → Sessions close cohort | Capture retains its inherited rendering. Numeric boot semantics reach capture/readback and pre-signal comparison. Missing inventory never establishes absence. | `test_process_identity_table`, close cohort and platform owned-process controls |
| SpawnCtl → cancelled-spawn birth fence | Retain exact comparison and close cohort kernel authority. An alternate liveness rendering alone cannot authorize pane destruction; availability refusal is deliberate. Remote quiescence and adoption are separate ordered work. | `test_start_presentation_consumers`, close/cancel controls |
| Spawn binding → ingest `_provider_root` | Retain nonempty exact birth plus generation/uid/executable/pane fences. A mismatch safely refuses ingestion; existing transcript-anchored rebind owns availability recovery. This is not a death/replacement claim. | `test_ingest_provider_root_binding`, `test_ingest_identity_recovery` |
| Ingest rebind / Store CAS → observer binding | Persisted producer unchanged. Existing independent transcript/inode/native-id and fresh-process fences remain; no liveness comparison is used to widen rebind authority. | `test_ingest_identity_recovery` |
| Native launcher inventory → provider occupancy / account executor | Retain exact birth attestation and complete native-image/uid/lineage/pane checks. Cross-environment mismatch safely refuses account mutation/repin. Host-local birth interpretations cannot be independently attested by the central verifier without additional protocol; this change adds none. Numeric Darwin boot freshness retains all other fences. Availability impact is explicit, not called unaffected. | `test_provider_cli_occupancy`, `test_account_repin`, provider account controls |
| Observer binding → producing-host queued-send / capacity probes | Retain exact birth plus uid/executable and transcript-source fences. Mismatch safely returns `provider_identity_changed`; no input, cancellation, death or signal authority follows. Relaxing these cancellation/input proofs from liveness alone would widen authority. | `test_queued_send_now_probe`, `test_codex_capacity_probe` |
| Main/deploy/coordinated/gate-owner readers | Already use explicit stable UTC/C observations. Unchanged controls; this work does not alter their producer/persistence contracts. | Existing gate-owner and deploy controls |
| Orphan reaper epoch conversion | No demonstrated timezone defect; unchanged. | `test_orphan_reaper` |
| Mobile shared lease/claim liveness | Separate source owner; no overlapping mobile edits or qualification claim here. | Mobile owner's accepted comparison and runtime evidence |
