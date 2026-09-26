# Local voice action delivery

The optional microphone service can return a version-1 typed spawn action alongside
its existing wake capture UUID and generation. The web client requests capability
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
audio. Older services and manual spawning preserve their prior behavior.

The web host uses the shared `web_cc` spawn/catalog RPC and `cc_handlers` routes. Enable the microphone in a private local web profile pointing at the existing loopback service; keep automatic microphone process spawning disabled. No Electron packaging or installed-client update is needed.


In web mode, microphone status and controls travel over the authenticated host
websocket (`mic:request`). The host forwards only existing microphone operations
to its configured backend; the page never contacts the viewer's loopback service.
The same-origin websocket check applies to microphone requests and recovery.
Mic Off, wake claims, generation checks and capture IDs keep their existing
behavior. The optional service remains a separate host-managed process.


A private profile may pin `mic.wakeTargetStreamId` when the intended assistant
shares its role with other sessions. Wake delivery follows only explicit forward
handoff links from that identity in the authenticated daemon snapshot, on the
configured `mic.wakeTargetHost`. Exactly one open target is required. Missing,
closed, ambiguous or cyclic lineage shows a no-target state and leaves wake
messages unclaimed. This setting grants no lifecycle authority and never resolves
identity from a display name. Without it, the existing unique role/host selection
continues to apply. Off and reconnect keep the same capture-generation and single
send rules.
