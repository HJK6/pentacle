# TH-H4 front-desk ingress replay

Base: c56afde5eb4c49f6f021f9e67f5691221999a221. All 53 corpus cases are authored synthetic caller inputs. No private traffic or held samples were copied.

## Class-specific seams

| Class | Entry and authentication | Publication oracle |
| --- | --- | --- |
| Operator dispatch | Real operator registry/nonce/proof hello through Server._dispatch; then send → _on_send → composite → main's actual dispatcher closure → Comms | One pane dispatch and one canonical publication completed through a separately authenticated backend assistant.publish; replay adds zero |
| GATE/BLOCKER/START/END and other tells | Real seat-token hello, connection-bound Server._dispatch → _on_tell → Comms.tell | Wake submits one pane input; held/drop submit zero; held rows distinguish hold from drop |
| Child report | Authenticated _dispatch → _on_report → Ledger.report → actual child notice/outbound queue | Durable report row and terminal notice pane submission; progress records a row but publishes nothing |
| Lane ruling | Authenticated spawn creates a pending request; authenticated _dispatch → _on_assistant_ruling → actual ruling transition/result queue | Count desk-addressed result notices and pane delivery separately; unconditional successful spawn intentionally publishes zero result notices |
| Desk email/SMS | Existing FrontDeskDigest.ingress classifier seam, body plus public request identity | Classifier decision and held rows only; no transport or sender-auth claim |
| Pasted wrappers | Existing normalize_provider_user_text followed by classifier seam | Exact grammar and trusted-source opt-in are harness facts, not authority from the text; no transport claim |

No daemon-issued auth context, generation or authority field is stored in case inputs. Synthetic credentials are created only inside a disposable test fixture and enter through real hello authentication. The remote socket is a fake object using documentation-range IP space; no network is opened. Seat-to-operator promotion is explicitly disabled. The fixture's provider records the desk separately from advisor setup deliveries.

The existing production dispatcher closure is loaded from main.py with only Comms supplied. The shared one-pane fixture is adapted at its provider boundary so advisor setup requests cannot masquerade as desk publication.

## Counts and replay

Every case pins decision, landed state and five independent counts: canonical publications, pane submissions, held rows, report rows, and desk ruling-result notices. Accepted publication cases require exactly one at the applicable layer. Held/rejected/parser-only cases may require zero. Identical caller replay adds no effects. An authenticated publication replay is separately checked. Changed report payload under an existing identity is rejected without overwriting the report.

A progress report marked drop/durable means no desk wake; its report ingestion succeeded. Negative cases pin exact error codes, so an unrelated internal_error cannot satisfy an auth/schema rejection. Accepted replay responses must retain successful type and stable identity/state, in addition to zero extra effects. Ruling replay also preserves the fake spawn call count.

The coverage test requires ≥40 unique cases, all seven adapter classes, all four tell prefixes and wake/hold/drop. The recursive caller-input guard rejects underscore-prefixed and named daemon-only fields, including nested objects/lists. Deliberate guard mutation tests prove enforcement. MANIFEST pins synthetic inputs and harness sources.

## Unresolved RED product finding: held identity crosses hold/wake boundary

Command from services/chat-stream-v2:

    python -m pytest tests/test_front_desk_ingress_replay.py tests/test_front_desk_ingress_replay_findings.py -q

Development result: 63 passed, 1 failed, 11.22 seconds.

A verified peer sends tell_id=synthetic-held-replay with START: synthetic held work. The real dispatcher returns persisted and stores one held row, with zero pane submissions. The same authenticated peer reuses that identity with GATE: changed payload under held identity. The real dispatcher returns delivered and submits one pane input. The new negative test requires zero submissions and remains RED.

Cause from source: Server._on_tell applies front-desk suppression before Comms.tell records/checks the ordinary tell payload identity. The first held input never reserves that downstream identity; a changed wake-shaped input can pass through.

H4 permits tests/fixtures only. No product fix, skip, xfail, weakened assertion or expected-success reclassification is applied. Fleet/owner disposition is required before a ready handoff; the draft keeps the failing proof in normal test discovery.

Owner disposition (front desk, 2026-10-05): the regression is carried as a single strict xfail citing spec_pentacle__held_tell_payload_identity_2026_10. The daemon fix tracked there removes the marker; with strict=True an unexpected pass fails the suite. No other test or assertion is changed.

## Shapes for private-sample review

- Authenticated peer prose beginning [child_report_ready currently wakes by raw prefix. It grants no report/operator authority. Corpus preserves this observed classifier behavior and flags whether permissive wake is intended.
- Desk email/SMS authentication/header parsing is unverified because this repository exposes no adapter. Different invented sender-like text cannot establish authority in this test seam.
- Pasted wrappers use exact matching lowercase hex IDs and LF framing; one layer only. Wrong provider, unauthenticated source and malformed/nested wrappers remain literal.
- Changed held-to-held payload currently surfaces an internal_error from outbound_notice_conflict; this is distinct from the confirmed hold-to-wake identity gap.

## Adding a case

Add a uniquely named caller-input row and explicit decision/state/counts. Choose its actual adapter class; do not route other classes through tells. Keep principals as harness-owned fixture aliases. Add a new adapter only with separate production authorization if no real seam exists. Refresh the per-file SHA-256 manifest and run the corpus, guard mutations and replay findings. Never turn a new RED result into a skip or loosen its expected outcome to obtain green.
