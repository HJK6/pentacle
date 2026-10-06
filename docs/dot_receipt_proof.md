# Late Dot receipt proof

The existing `send.receipt.get` request can carry an optional `original_request`
object with `logical_id`, `target`, `generation`, and integer `sequence`. Dot
builds it from its retained original attempt. The server checks the exact existing
Dot request-ID hash and the committed target and generation before looking for
delivery evidence. Missing or invalid proof preserves the ordinary receipt read.

Promotion requires one matching provider USER identity, the original target
lifecycle, provider and recorded timestamps at or after the claim, an exact
payload match, and no conflicting identity or receipt history. Matching copies
in the hot and archived stores count as one event. The archive opens read-only;
an absent archive is never created. Incomplete or over-budget lookup leaves the
receipt pending.

After archive lookup, the server rechecks generation, lifecycle, receipt identity,
and the existing event insertion watermark. Concurrent event activity can defer
promotion conservatively. A later ordinary read can try again through the existing
desk recovery path; this feature adds no poller or timer.

A proved landing appends one receipt and preserves prior history. Verified
reconciliation can reuse that landing only across identical pending rows;
terminal outcomes and changed identities remain authoritative. Ordinary receipt
reads retain their highest-row behavior. No replay or pane submission occurs.

The private logical identifier is used only to verify the request commitment.
It is not returned, logged, or passed to receipt storage. Public examples and
tests use synthetic preimages. Existing scoped-owner and visibility checks
remain in force. Dot applies its normal label only after a landed receipt.

Deploy the daemon before the additive caller change. An old caller continues
ordinary reads; an old daemon ignores the added object. The generic CLI receipt
read has no preimage and does not perform this promotion.
