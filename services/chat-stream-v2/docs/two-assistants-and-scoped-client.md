# Two named assistants, scoped client, Cosmo push, Daff recovery

This is the chat-daemon foundation for a second always-on assistant (**Daff**,
the operator's) living beside **Bart** on the same host, plus a restricted phone client
(**Cosmo**) that can use only Daff's chat. It is deliberately a *fixed two-assistant*
design — not a multi-user platform. Bart's behaviour is unchanged throughout.

## A. Named assistants (`bart`, `daff`)

An assistant "name" is just the prefix of its configured composite stream id;
nothing derives it in code. `AssistantCompositeConfig.all_from_env()` builds the
fixed map `{"bart": …, "daff": …}`:

- `bart` reads the existing **unprefixed** `PENTACLE_ASSISTANT_*` keys, so its
  config is byte-identical to the single-assistant daemon.
- `daff` reads `PENTACLE_ASSISTANT_DAFF_*` and stays inert (`enabled=False`) until
  those keys are configured; `PENTACLE_ASSISTANT_DAFF_ROLE` is its protected role.

`main.py` builds one `AssistantComposite` per name; `server.assistant_composite`
remains the `bart` alias so existing call sites are unchanged, and
`Server._composite_for(stream_id)` resolves the owning composite for any stream.

**Binding** is keyed by name: `v2_assistant_direct_binding(name TEXT PRIMARY KEY, …)`.
A one-time migration (`migrate_binding_to_named`, run in `Store._run`) converts the
legacy single `id=1` row to `name='bart'` verbatim; `rollback_binding_to_single`
restores the one-row form (used by the deploy rollback path). The mirror binding
is a per-composite dict; Bart keeps the legacy kv key `assistant.mirror.enabled`
while others use `assistant.mirror.<name>.enabled`.

**Policy** (`AssistantPolicy`) protects a *set* of roles (`roles` = primary +
`extra_roles`). `protects()` is membership, the singleton check in `available()`
is per-role (Bart and Daff never block each other), and `self.role` stays the
primary slug that still backs operator-credential and consent protection.
**Daff gets no fleet/lane authority** — that is governed solely by
`store_lifecycle_authority`, independent of role.

## B. Composite tell routing

`tell bart:assistant "…"` / `tell daff:assistant "…"` deliver to the composite's
*current* bound pane. `Server._composite_tell` authorises operator credentials or
agent stream tokens only (Dot/scoped clients get `dot_scope_denied`), resolves the
binding at send time, and checks the generation: a replaced pane (open at a
different generation) is refused `assistant_direct_generation_conflict`; a
dead/absent pane queues. Delivery reuses `comms.tell` with the routine
backend-ingress filter bypassed (`_assistant_composite_backend_dispatch`), so
`tell.ok` reflects the paste confirmation.

While the composite is unbound, the tell is durably queued in
`v2_assistant_composite_tell_queue` (in order) and flushed by
`Server._flush_composite_tells` after a (re)bind — from `_on_assistant_rebind` and
from Daff recovery.

## C. Scoped single-stream credential (Cosmo)

A credential may carry a server-authoritative `scope = {"stream": "daff:assistant"}`
(on the credential record and `ConnectionTrust`; **never read from the wire**).
Mint it with `operator_auth_cli.py issue --scope-stream daff:assistant` or an
enrollment link `mobile_enrollment_cli.py --scope-stream …`. Legacy records without
a scope load as unscoped (full rights).

A scoped connection is **deny-by-default**:

- A sticky `_client_scoped_connections` marker and a per-RPC registry recheck
  (`_scoped_credential_revoked`) mean a revoked credential is refused within one
  heartbeat and can never fall back to operator/loopback rights. Scoped
  credentials are never elevated to `operator_authenticated`.
- Only `SCOPED_ALLOWED_VERBS` are reachable (`hello`, `ping`,
  `request_stream_events`, `send`, `send.receipt.get`, `upload_blob_init/chunk`,
  `fetch_blob`, `transcribe_blob`, `register_push`); everything else →
  `scope_denied`.
- Every outbound frame is filtered to the one scope stream (`_frame_for_client`);
  `send`, `request_stream_events` and `send.receipt.get` are confined to it.
- **Blob/request ownership** (`v2_scoped_ownership`): an upload records the owning
  credential; `fetch_blob` requires ownership *or* that the blob is referenced by
  an event in the scope stream; `transcribe_blob` and `send` attachments require
  ownership; `send.receipt.get` requires the request id was issued by this
  credential.

## D. Cosmo push audience + push on Daff reply

Scoped `register_push` writes to a **separate** DynamoDB table `CosmoPushTokens`
(env `COSMO_PUSH_TOKENS_TABLE`), carrying `credential_id` + `scope_stream`, so the
legacy `PushTokens` readers never see the device. The table is declared in
`bartimaeus-triforce/infra/bartimaeus-infrastructure.yaml` (DynamoDB is managed by
CloudFormation per `aws_standards.md`).

When an assistant reply commits on a composite (`AssistantComposite.publish`,
prose/final), the `reply_push` hook (`cosmo_push.CosmoPush`) sends one Expo push
(title "Daff", first ~120 chars) to the stream's scoped tokens. Dedup is inherent:
`publish` fires the hook only on the first (non-duplicate) commit, so replays and
reconnects never re-push. Revocation is re-checked per send, `DeviceNotRegistered`
tokens are deleted, and the Expo transport is injectable (tests stub it). The hook
is wired to all composites but self-filters by stream, so Bart never pushes to the
Cosmo audience.

## E. Daff seat recovery

The reconciler preserves a dead protected seat ("do not spawn here"); it now also
fires `on_protected_dead`. `recovery.DaffRecovery` handles **only** Daff (it
declines any non-Daff row, so Bart's preserve path is untouched and Bart still has
no auto-respawn). Under a single-owner in-flight guard it:

1. spawns a fresh Daff seat via a daemon handoff spawn
   (`daemon:scheduler`, `claude-opus-5-5`/high, role=daff, startup prompt from
   `PENTACLE_ASSISTANT_DAFF_STARTUP_PROMPT`);
2. rebinds `daff:assistant` with the daemon-owned `store.recover_assistant_binding`
   (the normal handoff-proof path can't authorise against a dead predecessor);
3. flushes queued composite tells, in order, and wakes the composite worker.

It retries 3× with backoff (30 s / 2 m / 10 m); on exhaustion it marks the seat
degraded (status-card `update`) and tells `bart:assistant` exactly once. The Daff
chat history lives in the daemon store and survives the seat change, so a fresh
handoff seat is continuous; a plain Claude `--resume` is intentionally not used
(the resume path refuses a role/handoff spawn).

Provenance: `spec_pentacle__daff_assistant_foundation_2026_10`.
