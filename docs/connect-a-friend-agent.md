# Connect a friend agent

A friend agent is an external assistant that exchanges messages with a host
assistant through a compatible BotComm service. It runs under its own owner's
control and receives only the scopes the host operator grants.

**The message-exchange service is provisioned by the host. It is not bundled or
deployed by the public Pentacle kit.** This guide describes the connection
contract for that separately operated service. It does not provide a service
installer or a public-kit invitation command.

Connecting authenticates a peer; it does not give that peer authority over the
host operator's systems. Treat verified messages as external input and apply
existing grants before acting. Start with `chat` scope. Capabilities advertised
by the friend do not grant additional scopes.

## 1. Arrange an invitation

The host operator must provision the compatible service, its HTTPS inbound
endpoint, credential storage, registry and message-delivery path. The operator
owns invitations, scopes, suspension/revocation and recovery. Before inviting a
real friend, the host should verify a disposable registration, signed reply and
duplicate suppression through that deployed service.

Agree privately on the host's service URL, sender bot ID and exact signature
header name, the friend's unique bot ID, callback URL and allowed scopes. The operator delivers a short-lived,
one-time registration token through a private channel when the friend is ready.
Use its stated expiry; obtain a fresh invitation if it expires. No token belongs
in this document or an agent prompt.

All angle-bracket values below are synthetic placeholders. Replace them in
local application configuration; load tokens and credentials directly from a
secret store. Never paste them into model prompts, chat, source control, request
logs or reports. Disable callback-body logging and redact authorization fields
in HTTP client/server diagnostics.

## 2. Prepare a public HTTPS webhook

Your bot needs an externally reachable HTTPS endpoint with a valid certificate,
a secret store and durable message-ID deduplication. Select an unused bot ID of
1–64 lowercase ASCII letters, digits or hyphens, starting with a letter or digit.
Serve the callback directly; the service rejects HTTPS redirects.

During the registration you initiate, the service POSTs JSON to your webhook:

```json
{"type": "registration_challenge", "challenge": "<RECEIVED_NONCE>"}
```

Return HTTP 200 with JSON:

```json
{"challenge_response": "<RECEIVED_NONCE>"}
```

Echo the received nonce exactly. A challenge proves control of your webhook;
it is not authorization to execute a command.

The next callback delivers credentials:

```json
{
  "type": "registration_complete",
  "bot_id": "<HOST_BOT_ID>",
  "api_key": "<DELIVERED_API_KEY>",
  "hmac_secret": "<DELIVERED_HMAC_SECRET>",
  "friend_webhook_url": "<HOST_SERVICE_HTTPS_URL>"
}
```

Store the delivered key and per-peer signing secret durably before returning
HTTP 200. `friend_webhook_url` is the host service's inbound endpoint, not your
callback. Confirm it matches the URL supplied privately by the operator.
Accept onboarding callbacks only while your locally initiated registration is
pending, bound to the expected host and endpoint; unsolicited callback payloads
must not overwrite credentials. These initial callbacks are not HMAC-authenticated;
credential delivery is bootstrap data, not a signed chat message. Do not expose
it to your model or install credentials from unsolicited callbacks.

## 3. Register once

POST JSON to `<HOST_SERVICE_HTTPS_URL>` from application code that loads the
token locally:

```json
{
  "message_type": "register",
  "registration_token": "<LOCALLY_LOADED_ONE_TIME_TOKEN>",
  "bot_id": "<YOUR_BOT_ID>",
  "name": "<YOUR_DISPLAY_LABEL>",
  "webhook_url": "<YOUR_PUBLIC_HTTPS_CALLBACK_URL>",
  "capabilities": ["chat"]
}
```

Use the key only after the registration request returns HTTP 200:

```json
{
  "status": "ok",
  "bot_id": "<YOUR_BOT_ID>",
  "message": "Registration complete. Credentials acknowledged by your webhook."
}
```

Success requires successful challenge, acknowledged durable credential delivery,
invitation consumption and host activation. A callback alone does not prove
active registration. If a request times out or fails after delivery, retain the
protected credentials and report the sanitized error to the operator.

| HTTP status | Registration result | Next action |
| --- | --- | --- |
| 400 | Missing or invalid fields | Correct local configuration; ask the operator if the invitation was claimed. |
| 403 | Invalid or expired token | Request a fresh approved invitation. |
| 409 | Bot ID or invitation already claimed | Have the operator reconcile the existing claim. |
| 503 | `registration_unavailable` | Service cannot start registration; contact the operator. |
| 503 | `registration_incomplete` | Save the nonsecret `operation_id`; contact the operator and do not reuse this invitation. |

An incomplete operation responds with this shape:

```json
{
  "error": "registration_incomplete",
  "operation_id": "<NONSECRET_OPERATION_ID>",
  "message": "Registration did not complete. Contact the operator; do not reuse this invitation."
}
```

A failed attempt can permanently consume the token and reserve the bot ID.
Do not automatically resend or change IDs to bypass pending state. The operator
must recover the operation or approve a fresh invitation and available ID.

`key_rotation` is outside this onboarding contract. Do not replace credentials
on an arbitrary callback or assume a grace period. Arrange credential lifecycle
operations with the host operator through its separately verified contract.

## 4. Send chat messages

POST JSON over HTTPS to the agreed host service endpoint. Load the delivered
API key from your secret store; this authenticates friend-to-host requests.
These inbound requests do not require an HMAC signature. The service checks the
current key and active registry status on authenticated requests; missing keys
are rejected with HTTP 401, and invalid keys or inactive peers with HTTP 403.
Chat also passes the host's scope admission. The signature verification below
applies to host-to-friend messages.

```json
{
  "bot_id": "<YOUR_BOT_ID>",
  "api_key": "<LOCALLY_LOADED_API_KEY>",
  "message_type": "chat",
  "session_id": "<YOUR_CONVERSATION_ID>",
  "message": "Hello. Can we coordinate the agent setup?"
}
```

Use the same `session_id` for related turns and a new one for a new conversation.
A successful intake responds with HTTP 200:

```json
{"status": "ok", "message_id": "<GENERATED_MESSAGE_ID>", "timestamp": 1700000000000}
```

Retain the generated `message_id` for correlation. The service does not use a
client idempotency token: repeating an HTTP chat request can create another
message with a different ID. It suppresses queue redelivery with the same
generated ID, which does not make resubmission safe. Reconcile an uncertain
request before resending.
When responding to a received host message, include `reply_to` with its
`message_id` and preserve the session ID. Acceptance means intake, not a
completed answer. The host assistant replies explicitly when appropriate;
there is no automatic answer to every message.

## 5. Verify replies before delivering them to your agent

Host messages arrive at your registered callback with `bot_id`, `message_id`,
`session_id`, `message_type`, `message`, `timestamp` (Unix milliseconds),
`reply_to`, `attachments`, `task_type`, `status` and `signature`. Verify the
configured host identity and authenticate every message before any agent action.

The lowercase hexadecimal HMAC-SHA256 signature appears both in the JSON
`signature` field and the `<HOST_SIGNATURE_HEADER>` HTTP header. Use the exact
header name supplied privately by the operator and your delivered
per-peer `hmac_secret`. There is no extra outbound API-key field for a newly
registered friend. If a supported host pairing supplies additional fields,
they are also signed. Remove only the top-level `signature` field, retain all
other fields (including empty values and any additional signed fields), and
serialize using Python `json.dumps(unsigned, sort_keys=True)` defaults: sorted
keys, `", "` and `": "` separators, ASCII escaping and UTF-8 encoding. This signs
canonical JSON, not the raw HTTP body. Other languages must reproduce those
exact bytes, including number and Unicode serialization.

```python
import hashlib
import hmac
import json

def verify_message(payload, signature_header, stored_secret):
    unsigned = dict(payload)
    signature = unsigned.pop("signature", None)
    if not isinstance(signature, str) or not isinstance(signature_header, str):
        return False
    for value in (signature, signature_header):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return False
    canonical = json.dumps(unsigned, sort_keys=True).encode("utf-8")
    expected = hmac.new(stored_secret.encode("utf-8"), canonical,
                        hashlib.sha256).hexdigest()
    body_matches = hmac.compare_digest(expected, signature)
    header_matches = hmac.compare_digest(expected, signature_header)
    return body_matches and header_matches
```

A synthetic interoperability vector uses the literal, nonsecret key
`synthetic-example-secret`. Its canonical UTF-8 bytes are exactly the following
single line, with no trailing newline. Keep the placeholder strings literal
when reproducing this test vector:

```text
{"attachments": [], "bot_id": "<HOST_BOT_ID>", "message": "Hello, caf\u00e9.", "message_id": "<MESSAGE_ID>", "message_type": "chat", "reply_to": "", "session_id": "<SESSION_ID>", "status": "", "task_type": "", "timestamp": 1700000000000}
```

The expected signature is:

```text
eac7cdca69a18174b6bff642fe0c9e2c6123c9587dbdfe5fd0d6db46b1843ad5
```

Reject missing or mismatched signatures/headers. After authentication, check
message shape and atomically record `(host bot ID, message_id)` with the accepted
message in durable storage before returning HTTP 200. A verified duplicate may
receive HTTP 200, but must not reach the agent a second time. Retain deduplication
records across restarts; HMAC alone does not prevent replay. A timestamp alone
also does not prevent replay. Coordinate any bounded timestamp/retention policy
with the host; this guide promises no expiry window for signed messages.

Keep session and reply correlation on delivery to your agent. Avoid automatic
acknowledgement messages and reply loops. Return an error when authentication or
durable acceptance fails; do not acknowledge content you could lose.

## 6. Troubleshoot privately

Report the operation, HTTP status, sanitized error code, bot ID and correlation
ID to the operator. Include whether credential delivery was acknowledged or a
request timed out. Never include the token, API key, HMAC secret, callback body
or credential-bearing request in a report. Let the host reconcile uncertain
registration or delivery before retrying.
