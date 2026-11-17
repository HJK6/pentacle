# Assistant owed items

The daemon keeps a composite-owned record of explicit follow-up work. It does not infer promises from prose. Records survive front-desk rebinds. Only the current verified front desk and an operator credential may write; the current ruling advisor may also read.

## Publish or recognize work

Both `agent-orch assistant publish` and `agent-orch send-file` accept:

- `--owes none`, or `--owes "a short label"`
- `--owes-due +2h` (also minutes/days, or an offset-bearing ISO-8601 timestamp)
- `--owes-spec <id>` or `--owes-lane <id>`
- Repeatable `--closes <owed-id>`

A label opens one item in the same transaction as the publication. A final prose reply can close open items as delivered; a proactive result note can do so too. No flags are added to stored publication payloads or route rows. The new `v2_assistant_owed` table holds all links. Repeating a publication key with the same flags or neither flag returns the original IDs without repeating effects. Conflicting effects refuse and roll back the entire publication.

Only assistant publish supports a batch reply. Its item references the head message and records every member. Send-file remains single-reply only.

For work recognized later:

```sh
agent-orch assistant owed open --label "return the result" --message-id <id> --due +2h
agent-orch assistant owed list
agent-orch assistant owed show <owed-id>
agent-orch assistant owed close <owed-id> --delivered-by <publication-key>
agent-orch assistant owed close <owed-id> --withdrawn-by <later-operator-message-id>
agent-orch assistant owed close <owed-id> --superseded-by <later-operator-message-id>
agent-orch assistant owed close <owed-id> --void --reason "This item was opened in error"
```

The primary composite is the default; `--composite-stream-id` selects another. Open also accepts `--publication-key` instead of `--message-id`. Repeated explicit opens with the same source and label return the original item, even after its due time. Labels are trimmed plain text, 1–160 characters; control characters are refused. A void reason requires 20–300 characters. Times normalize to UTC; a newly assigned deadline must be future. Spec and lane IDs are labels, not authority grants or external-system lookups.

Delivered evidence must be a publication of the same composite newer than the source. Withdrawal/supersession must cite a later operator-written message of that composite. Source and evidence messages are read through the retention history reader. Show returns their original stored text, never a retyped copy. `list` shows open items by deadline (overdue first), then oldest; undated items follow dated items. `--closed --since <offset-bearing-time>` excludes voids; `--void` lists voids separately.

An `owed:<id>` proactive reference is allowed on result, problem, reminder and other notes. A reference alone does not close anything.

## Due delivery and switch

Each outbox pass queues an overdue open item for the currently bound desk. The durable notice identity includes item, recipient and generation. Its guard retires stale recipients and closed items. Successful delivery records `due_notice_at`; tick also reconciles durable outbox completions if a process stopped before the delivery callback. With no binding, nothing is queued. These notices bypass the digest but retain normal outbox delivery safety.

`PENTACLE_ASSISTANT_OWES_REQUIRED` defaults off. When enabled, a final prose reply to an operator-written dispatch must provide `--owes`. All batch members must be operator-written for this requirement. Desk dispatches, scoped or outside-assistant input, status acknowledgments, questions, other reply kinds and proactive notes do not trigger it. Other owed features work with the switch off. Existing-key retries are compared before this requirement is evaluated.

## Validation and release boundary

Unit tests cover transactional open/close, replay, authorization, evidence, archived messages, refs, due state and a prior-release reader. Socket smoke tests run an isolated daemon child with the actual server/store/outbox/CLI and a synthetic provider, covering publish and send-file. They require the repository's isolated tmux test namespace even though no model is invoked.

This source change does not enable the switch, publish guidelines, migrate live records or authorize deployment. Rollback leaves the additive table unread and preserves all existing row shapes; disabling the switch removes its sole new mandatory-field refusal.
