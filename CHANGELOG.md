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
