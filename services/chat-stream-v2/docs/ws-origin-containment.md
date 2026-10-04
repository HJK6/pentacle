# WebSocket Origin containment (H1)

The daemon trusts a loopback peer: `_is_loopback_client` reads the real transport
`remote_address` and the non-loopback verb gate exempts loopback connections from
authentication for a set of verbs (including `fetch_blob`). That exemption is for
*local daemon clients* (agent-orch, the web-service bridge). A **web page running
in a browser on the host** is a different principal: it can open
`ws://127.0.0.1:<port>` directly, bypass the Origin-enforcing web bridge, and —
because the socket is loopback — reach the exempt verbs with no credential
(F1 risk #2; P1 confirmed a raw loopback peer with a foreign `Origin` read a
referenced blob anonymously).

## The guard

`_reject_browser_origin` is installed as the `process_request` callback on **every**
daemon WS listener — the plain fleet listener (`bind`) and the daemon-terminated
TLS/Dot listener (`_bind_tls_listener`). It runs during the opening handshake,
**before** `_handle_client` (and therefore before the welcome frame, hello
activation, the snapshot, any broadcast, and any RPC).

It **default-denies** (HTTP 403, handshake aborted) any upgrade whose headers
carry, matched case-insensitively on the header **name** only:

- any `Origin` header — including `null`, empty, malformed, or **duplicated**
  values (the value is never inspected: an empty allowlist means no value could
  make an `Origin` acceptable); or
- any `Sec-Fetch-*` header (a browser cross-origin signal even when no `Origin`
  is present).

The allowlist is **empty** — no Google / localhost / tailnet / web-UI exception —
and there is **no `X-Forwarded-*` identity exemption**: a forwarded header is a
wire claim, not a transport fact. A denial logs the header **name** and peer IP
only (never the `Origin` value).

Browsers always send `Origin` (and `Sec-Fetch-*`) on a WebSocket upgrade;
legitimate daemon clients do not:

- **agent-orch** — python `websockets.connect(...)` with no `origin=`/
  `additional_headers`;
- **web-service bridge** — node `new WebSocket(url)` with no `origin`/headers;
- **native mobile** — sends neither.

So the guard denies the browser vector while leaving every supported client
untouched. The Dot external principal reaches the TLS listener over the same
agent-orch client (see [dot-external-principal.md](dot-external-principal.md)) and
is likewise unaffected.

## What H1 does NOT do

H1 is a deny-browsers handshake guard, not an authentication mechanism. A
**no-`Origin`** client on loopback still receives the blanket loopback exemption
with no credential — closing that is **H2** (durable loopback auth for sensitive
verbs), a separate change. H1 also does not UID-scope the loopback exemption (H3).
