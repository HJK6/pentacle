# Operator authentication: public verification guide

The websocket handshake may run unauthenticated for a loopback development daemon. A deployment that enables operator authentication must keep proof material outside the repository and must test only the public protocol boundary.

## Credential model

An operator credential has a generated id, a client kind, a label, creation metadata, and a proof key held by the credential store. The client receives an enrollment envelope; the envelope is not a reusable password and must never be printed into test evidence.

The server challenge contains a nonce, protocol version, and expiry. The client proof binds the nonce, credential id, and client kind with HMAC-SHA-256. A missing proof produces an untrusted connection; an invalid proof produces a typed authentication error.

## Synthetic enrollment

For tests, use a temporary registry under a test-owned directory and generate a fresh key for each run:

```bash
mkdir -p /tmp/pentacle-example/auth
chmod 700 /tmp/pentacle-example/auth
```

The exact CLI flags are owned by the checked-in authentication tool. Use its `--help` output, pass a generated temporary registry, and replace any displayed proof with `<redacted>` before storing output. Never use a personal device label, a real email, or a shared household credential in a fixture.

## Verification matrix

Test the following cases with a synthetic client:

- valid proof for the expected client kind;
- expired challenge;
- unknown or revoked credential;
- changed nonce, client kind, or credential id;
- malformed envelope; and
- reconnect after revocation.

Assertions should inspect error codes and trust state, not secret material. A successful unit test proves implementation behavior; it does not enroll a real device.

## File handling

If a local client reads an envelope from a file, require a regular file, owner-only permissions, and a private parent directory. Empty or absent files should leave the connection untrusted. Keep the path configurable and use a generic example such as `$XDG_CONFIG_HOME/pentacle-example/token`.
