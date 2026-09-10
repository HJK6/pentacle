# Child sessions and retained exchanges

The daemon models a child session as a normal session with a parent reference. The parent relationship is metadata used for visibility and notification routing; it does not grant access to arbitrary sessions.

## Child lifecycle

1. A caller requests a child with a unique `request_id`.
2. The daemon validates the caller's ownership and the requested host id.
3. The child receives a synthetic stream id such as `coordinator:fixture-child`.
4. The parent receives a typed admission result and later lifecycle events.
5. The child reports completion, a blocker, or an error before it closes.

Example fixture:

```json
{
  "parent_stream_id": "coordinator:fixture-parent",
  "child_stream_id": "coordinator:fixture-child",
  "state": "starting"
}
```

## Retained exchanges

Requests and replies are retained for the lifetime of the local store so reconnecting clients can rebuild a bounded view. A request is correlated by `request_id`; repeated registration with different material is rejected. Retention is not a promise to retain user transcripts forever: configure a disposable store and clear it after tests.

## Notifications

Parents may subscribe to `end`, `blocker`, `idle`, or `quiet` notices for their direct children. A notice has one immutable identity, a trigger timestamp, and a delivery state. Repeated notices require an explicit repeat policy and a new activity baseline.

Closing a child retires pending inactivity work. An accepted final report can still reach the original parent generation, but a replacement parent cannot inherit that notice. These rules prevent duplicate alerts and stale ownership.

## Test guidance

Use host ids such as `coordinator`, request ids such as `fixture-request-1`, and deterministic timestamps. Test reconnect, duplicate registration, child close, and parent replacement without using live provider sessions or private stream identifiers.
