# Provider paste display text

`provider_wrappers.py` owns the exact provider wrapper table. The Claude JSONL
normalizer opts in at the existing source-authenticated transcript boundary. It
recognizes the whole string below (literal LF framing, paired ASCII decimal IDs):

```text
\n\n<pasted_content id="123">\nBODY\n</pasted_content id="123">\n
```

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

In a coordinator-approved runtime window, `tools/provider_wrapper_probe.py`
executes the existing Claude prompted/promptless fleet cells for supplied hosts.
It adds a real tell per cell, checks submission confirmation and the exact
post-watermark USER display/tag/raw event, records provider PID/source bindings,
and uses the fleet harness's generation-fenced cleanup. Required arguments are
`--hosts`, `--evidence-dir`, `--candidate-sha`, `--daemon-pid` and
`--runtime-checkout`. It stops on the first failed cell without retry, preserves
raw evidence and records closed/expected ownership counts. Assistant-marker
smoke success alone does not prove wrapper normalization or submission landing.
