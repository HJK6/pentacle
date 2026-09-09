# Durable send-receipt design note

The send path should expose one durable receipt for each accepted request. The receipt is appended with the existing user event and can be read by a later client to reconcile an optimistic row.

Governance scope: subtractive/compatibility-only
Governance ruling: necessity=make delivery state observable; reused substrate=existing event append and pull query; authority=repository review

The design adds no background actor, retry loop, token, or private endpoint. A receipt contains a request id, optimistic id, bounded status, and server timestamp. Duplicate request ids resolve to the existing receipt; an explicit retry creates a new request id. Tests use a temporary database and synthetic text.
