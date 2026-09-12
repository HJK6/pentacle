# Local voice action delivery

The optional microphone service can return a version-1 typed spawn action alongside
its existing wake capture UUID and generation. The desktop requests capability
`actions_version: 1`, validates the proposed model/effort against the live daemon
catalog, and uses its existing enrolled spawn connection. Initial prompt, bounded
objective and stable `voice:<capture-id>` idempotency key are forwarded as the
existing daemon fields; no new authentication or daemon protocol is introduced.

An action never falls back to a Bart chat message. Ordinary wake conversations
continue resolving the current protected assistant across handoffs. Local spawning
needs a connected daemon, but does not need Bart's assistant session to be present.
Mic Off or a generation change cancels before admission, including while the catalog
is loading. A spawn already admitted cannot be retroactively cancelled by Off.
A queued result is distinguished from a started session; uncertain outcomes are
reported without creating a new automatic request/key. The service receives a
bounded outcome enum for local speech, not arbitrary renderer-supplied speech text.

The service owns interpretation, quote data and short spoken feedback. The public
client includes no private endpoint, model credential, speaker destination or raw
audio. Older services and manual desktop spawning preserve their prior behavior.
