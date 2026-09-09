# Routing integrity compatibility note

This public note records a compatibility-only change: the client no longer treats a live routing observation as authority to mutate unrelated state.

Governance scope: subtractive/compatibility-only
Governance ruling: necessity=remove ambiguous routing authority; reused substrate=existing request and notification records; authority=repository review
Deletion ratio: descriptive only; measure the actual patch

The implementation may retain an immutable event for diagnostics, but routing facts are read-only inputs. A missing or conflicting route must produce a typed warning and require an explicit caller decision. No private work-item lineage or deployment procedure is required to use this rule.
