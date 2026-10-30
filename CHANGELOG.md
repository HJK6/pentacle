# Changelog

User-visible changes to Pentacle, newest release first. Each dated section is one
promoted release. Its heading date is the UTC date the section was cut on the
candidate branch; a later cut on the same date adds ` (2)`, ` (3)`. The exact
commit is the annotated `v2-gate/<sha>` tag that promotion created for that
candidate, the same SHA shown as the build and runtime SHA in Settings. List
them with `git for-each-ref --sort=-creatordate refs/tags/v2-gate/`. A release
with no user-visible change carries the single bullet `Internal changes only.`

This file starts at the release that introduced it; earlier history is not
backfilled. Changes that land between releases accumulate under `Unreleased`.

## Unreleased

### Added

- Opt-in release seam admission unions installed baselines, rejects stale or incomplete proof, and keeps affected canary invocation and verdict scopes aligned. One finite client wire/render/model tuple is registered; other surfaces remain unqualified.
- Advisory change-to-test reports name affected boundaries, existing test targets and coverage gaps without changing required gates.
- Installations can hide whole providers from advertised New Chat choices with empty `available_models` lists; web respects visibility and saved-choice fallbacks while explicit launches stay compatible. Legacy mobile UI support requires a later app release.
- Daemon stats show current admission usage and held verbs with opaque holder ids; diagnostic reads have isolated bounded capacity that cannot consume ping credits.
- Front-desk account health revalidation with an exact native identity proof, revision fence and durable audit receipt; ordinary probes keep existing health flags.

### Fixed

- Long assistant message text wraps within the chat width; image paste keeps accompanying text and uses the existing attachment preview and send flow.
- Existing unbound sessions keep accepting input before the first fleet CLI pin; initialized or inconsistent pin state still refuses that input.
- Initial fleet CLI pin activation prepares and proves every serving home before enforcing the pair, preserving compatible running seats. Home-scoped maintenance fences survive restarts and expiry, with authenticated proof-backed recovery and managed launcher attribution.
- Oversized retained exchange receipts no longer crash daemon startup; repair processes each oversized receipt in its own checkpointed transaction.
- Assistant backing sessions stay out of the normal chat list across spawn, resume and recovery while their composite chat remains available.
- Central Codex usage collection follows the host's healthy default account after account switches and redeploys, retaining stale data honestly when the default is unavailable.

- New image sends with decomposed Unicode captions retain their caption and image after chat reload.

- Web page-load reads leave request capacity for image uploads; queued user actions take priority over background reads, and image uploads verify the complete file digest before reporting success.

- Image sends with canonically equivalent Unicode captions reconcile into one caption and image bubble even when the native echo arrives before the send result, instead of leaving a second row marked sending.
- Discarded voice transcriptions no longer block image uploads or ordinary chat controls. Busy uploads and voice takes show readable errors and keep unsent content for a manual retry.
- Repair-review reports can explicitly bind their typed commission message id, preserving distinct reject counts when reports use message 0.
- Claude account probes wait for native cache refresh eligibility; stale usage no longer marks a verified account as an identity conflict.

## 2026-10-23 (3)

### Added

- Optional local Gemini CLI sessions, native recording ingest and prompt-submission predicates, contributed by @Rohan-Juneja. Setup and compatibility limits are documented in `docs/gemini_provider.md`; validation uses fixtures and a fake provider, with live Gemini compatibility unverified.

## 2026-10-23 (2)

### Added

- Work ownership reports flag unattended implementation and include cached disposition context when a commissioned agent finishes.

## 2026-10-23

### Added

- Optional named provider accounts add explicit spawn selection, per-host defaults, account usage and targeted re-login; disabled by default.

### Changed

- Agent chats send normally from the composer. Queued messages show their delivery state with a Send now action that interrupts the current turn without sending the message twice; the Queue/Steer chooser is removed.

## 2026-10-21

### Added

- Chat daemon: sends from configured desk credentials are labelled `[from <desk>]`, so desk traffic is attributable and each desk can be revoked on its own.

### Changed

- Web host in token mode: WebSocket upgrades must come from the host's own origin (or carry none from a local client). Repeated failed logins from one peer back off, and the README recommends a generated token.

### Fixed

- Chat daemon: a new seat whose first prompt was delivered while the event store was briefly unreadable is no longer left in `starting` or marked as never started.

## 2026-10-20 (2)

### Added

- Steer now, Queue and a Stop control that stays visible on agent chats while the agent works (web and desktop). Steer now interrupts the current turn and sends your message at once; Queue runs it after the turn. The assistant composite chat is unchanged.
- Recurring schedules: durable agent-orch rules with an explicit time zone (`agent-orch schedule recurring ...`), listed in a Scheduled section in the web and desktop sidebar, where a rule can be paused, resumed and edited.

## 2026-10-20

### Added

- This changelog. Release promotion now refuses a candidate whose changelog was not cut for it.
