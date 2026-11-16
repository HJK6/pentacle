# Session attribution and reminder eligibility

This document describes the public lifecycle and test contract for associating
provider activity with a local session. The rules apply to any provider or
host adapter and do not depend on a particular machine layout.

## Lifecycle and provider binding

An adapter records its executable, process birth marker, and transcript root
when a supported tuple is launched. The observer accepts a provider root only
when those launch-time values still match. Missing birth proof, an unexpected
executable, or a changed process identity fails closed.

The first fallback bind requires one unique writable provider transcript. The
opened descriptor's device and inode identity are retained with the session.
An unrelated transcript, a read-only descriptor, or a changed file identity
cannot replace that binding. The same proof is used after a restart or a
temporary writer gap.

## Generations and events

Session identity and file identity are checked against the current lifecycle
generation. Events from an earlier generation may be displayed as history, but
they cannot qualify new engagement. The additive observer metadata does not
rewrite historical events.

Only current-generation operator `USER` turns count toward reminder eligibility.
Blank, peer, sidechain, synthetic, and reminder envelopes do not count. Tool
and assistant activity may update presence, but does not satisfy the operator
turn requirement.

## Reminder eligibility

Each provider adapter waits for two admitted current-generation operator turns
before sending a title or status reminder. A successful reminder starts a
durable cooldown that survives a process restart. Activity from the operator
re-arms the cooldown; unrelated activity does not.

Visible top-level sessions must be known idle before a reminder is sent. Busy,
hidden, child, and unknown sessions remain silent.

## Presence authority

Local mirror proof and daemon presence capture are separate trusted sources.
When remote capture is enabled, a successful capture records the current
`capture_generation`. A failed, unknown, offline, or dead capture revokes
eligibility. A generation change also revokes it, including captures that were
already in flight. A pane reappearance therefore needs a fresh, matching
capture; an inconclusive inventory never asserts death.

Presence data from a host adapter cannot copy eligibility flags into the live
session overlay. A generic remote adapter may use an address such as
`example.local`, but network reachability is not a substitute for lifecycle
proof.

## Public smoke fixture

The provider smoke adapter is self-contained and writes synthetic JSONL events
under the directory named by `CHAT_STREAM_STUB_ROOT`. The directory should be
temporary and writable. Each session gets a workspace-scoped transcript, and
the emitted records contain only the supplied session id and test input.

The public tests cover writable and read-only descriptor controls, lifecycle
and generation revocation, provider/host turn matrices, durable report
delivery, and cleanup of temporary state. Run them with the repository's
standard Python test command; the fixture is self-contained.
