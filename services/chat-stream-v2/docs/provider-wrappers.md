# Provider paste display text

`provider_wrappers.py` owns the exact provider wrapper table. The Claude JSONL
normalizer opts in at the existing source-authenticated transcript boundary. It
recognizes exactly two full-string shapes (literal LF framing, paired nonempty
lowercase ASCII hexadecimal IDs (`[0-9a-f]+`)):

```text
\n\n<pasted_content id="123">\nBODY\n</pasted_content id="123">\n
<pasted_content id="123">\nBODY\n</pasted_content id="123">
```

IDs must match exactly, preserve leading zeroes, and have no fixed length.
Uppercase, non-hexadecimal and empty IDs are rejected.

Only one outer layer is removed; BODY bytes are preserved. Similar or malformed
text, other providers and callers without authenticated-source provenance are
unchanged. Exact literal operator text is indistinguishable from this grammar;
the tag records grammar provenance, not cryptographic provider authorship.

The additive wire contract is:

```json
{"text":"BODY","provider_wrapper":{"kind":"claude_pasted_content","id":"123","provenance":"grammar"},"raw":{"provider_content":"exact original wrapped text"}}
```

Unmatched events omit both added fields. Existing raw metadata and event identity
remain intact. String and joined text-block USER records use the same transform;
assistant and tool-result content do not. Unwrapping precedes existing peer and
synthetic classification, preserving TELL sender/anchor and notice behavior.
Human `queued_command` attachment prompts share the same unwrap, but retain
kind USER, subtype `queued-command`, UUID/index, `queued_at` and the existing
timestamp adjustment. Queued peers and synthetic text keep USER identity so
replay cannot create TELL/SYSTEM copies. Notice matching uses the unwrapped
text first: an inner registered notice receives its notice-kind tag; wrapped
prose receives `claude_pasted_content`. Raw provider bytes remain unchanged.

At this source boundary, a `type=user` record is suppressed only when both
`isMeta is True` and `turnCompanion is True`, content is a string, and it fully
matches this coordinate-bookkeeping grammar:

```text
\[Image: original [0-9]+x[0-9]+, displayed at [0-9]+x[0-9]+\. Multiply coordinates by [0-9]+(?:\.[0-9]+)? to map to original image\.\]
```

Identical operator text, false/missing flags, other metadata, attachments and
Read tool-result events retain their existing behavior. Source JSONL remains
the audit record; this is not a body sanitizer.

Landing proof and receipt projection consume display text; attachment projection
keeps its existing digest authority. Chat-core's `providerWrapper.ts` mirrors the
grammar and Python submission whitespace/ANSI normalization. Tagged text is
already display text and is never unwrapped again. The legacy fallback requires
a Claude USER event with structured Claude-JSONL source metadata from the daemon.
Optimistic ID, message ID, stream and timestamp guards retain their priority.
No public `src/index.ts` export changes are required.

The shared fixture is `pentacle-chat-core/tests/fixtures/provider-wrapper.json`.
Focused coverage is `tests/test_provider_wrappers.py` and chat-core's
`tests/providerWrapper.test.ts`. The wrapper logger uses `subsystem=provider_wrapper`
and `bug_ref=spec_pentacle__claude_pasted_content_envelope_2026_09`, without prompt text.
Queued recognition and image suppression use the same logger with
`bug_ref=spec_pentacle__claude_queued_notice_and_image_meta_2026_09` and bounded
action names, without bodies.

In a coordinator-approved runtime window, `tools/provider_wrapper_probe.py`
executes the existing Claude prompted/promptless fleet cells for supplied hosts.
It adds a real tell per cell, checks submission confirmation and the exact
post-watermark USER display/tag/raw event, records provider PID/source bindings,
and uses the fleet harness's generation-fenced cleanup. Required arguments are
`--hosts`, `--evidence-dir`, `--candidate-sha`, `--daemon-pid` and
`--runtime-checkout`. It stops on the first failed cell without retry, preserves
raw evidence and records closed/expected ownership counts. Assistant-marker
smoke success alone does not prove wrapper normalization or submission landing.

Add `--journeys` for two disposable notification flows: prompted cells resolve
an idle ordinary answer; promptless cells resolve during an owned 45-second
Bash tool and require the genuine source attachment to be `queued_command`.
Each question is asked by its own temporarily visible seat through
`agent-orch prompt ask`; the authenticated operator harness resolves its saved
Done action once. The oracle requires durable delivery proof and one matching
post-watermark USER with the notification-kind tag. It then asks that seat to
Read an oversized owned PNG, captures the actual source metadata companion,
and checks the Read event survives with no phantom USER. The owned PNG and
generation-bound seats are cleaned up on failure. This probe runs only in a
coordinator-granted runtime window; source tests do not establish live proof.

For `spec_pentacle__claude_queued_notice_and_image_meta_2026_09` only,
`tools/history_repair.py` rehearses a bounded historical correction. An external,
reviewed scope packet selects one stream and generation, one source-proven
queued notification answer, and exact image metadata source UUIDs. It permits
only a queued USER text/raw/wrapper/tag rewrite and metadata-row deletion;
notice resolution and delivery state remain unchanged. This is a one-off tool,
not a migration registry. Real identifiers, source JSONL, database backups,
manifests and rollback preimages stay outside the repository.

`census` reads the database through SQLite read-only mode and its backup API.
`freeze` verifies the source bytes, backup digest and required
`--expected-scope-sha256`, then writes the fixed manifest externally.
`apply` and `rollback` require `--expected-manifest-sha256`, the frozen source
copy, external `--preimages`, and a coordinator-owned `--writer-quiescent`
window. Row identity, generation, content/key hashes, operator send bindings
and deletion references must match; writes recompute `event_key` in the same
CAS transaction. Rehearsal applies twice, verifies the second is a no-op,
then restores exact preimages and verifies row bytes. A scratch rehearsal is
not permission to write a running daemon's database. The coordinator receives
the exact hash-bound runtime commands with the external evidence packet.
