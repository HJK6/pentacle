# Report Assets

`report` is the only asset type accepted for new operator-reviewed agent documents in Pentacle. Its structured JSON supports readable, commentable sections, status, chips, tables, and callouts.

Historical Markdown and JSON-table assets remain readable so existing tabs do not break, but neither legacy type can be newly published. Convert every new review artifact to this report schema instead of falling back when validation fails.

## Publish Contract

Publish with `agent-orch asset publish --type report --content-file <json> [--asset-id <id>] [--spec-id <spec>]`. The daemon and CLI validate the same schema from `services/_shared/asset_schema.py`.

Top-level JSON:

```json
{
  "schema_version": 1,
  "title": "Review title",
  "sections": [
    {
      "id": "summary",
      "title": "Summary",
      "status": "reference",
      "blocks": []
    }
  ]
}
```

Required rules:

- `schema_version` is `1`.
- Every section has a unique stable `id`, a `title`, a `status`, and a `blocks` array.
- Every block has a unique stable `id`. Prefer keeping the same id for the same logical paragraph/list/table/callout, and give new content a new id.
- Comments are lightweight, per-revision feedback and do **not** carry across revisions: re-publishing an `asset_id` clears the prior comments (a revision is a fresh document). Within a single revision, if a commented block disappears before you re-publish, Pentacle shows the comment in the unanchored bucket with the saved excerpt.
- Payloads share the normal asset body cap and must not embed images.

## Vocabulary

Section statuses:

- `dispatched`
- `in_progress`
- `stalled`
- `blocked`
- `reference`

Block types:

- `para`: `runs`
- `list`: `ordered`, `items`
- `table`: `columns`, `rows`
- `callout`: `kind`, `title`, `runs`

Callout kinds:

- `info`
- `warn`

Run types:

- `text`: `text`
- `code`: `text`
- `link`: `text`, `href`
- `chip`: `text` or `chip`, plus optional `variant`, `kind`, `status`, `href`

Chip variants:

- `plain`
- `typed`
- `status`
- `link`

Chip kinds:

- `generic`
- `epic`
- `branch`
- `db`
- `database`
- `story`
- `file`

Chip statuses:

- `ok`
- `warn`
- `stop`
- `info`

Links must use `http://` or `https://`. Unknown keys are rejected by the validator so agents get actionable path-specific errors before the operator sees the asset.

## Feedback Loop

Reports are a lightweight way for the operator to hand feedback to the producing agent — not a formal approve workflow. The operator clicks a block (or its pin) to open a thread and adds/edits/deletes short comments; comment cards show just the text and a timestamp. Comment mutations — including agent-side resolves arriving over the daemon broadcast — update an open viewer (slot tab or pop-out) in place: no reload, no scroll movement; only a re-publish or review-status change replaces the rendered document. `Send to chat` delivers a pointer tell to the producing stream so it can fetch the feedback:

```bash
agent-orch asset comments <asset_id> --unresolved
```

Agents may mark addressed comments (optional; the operator does not resolve in the UI):

```bash
agent-orch asset comments resolve <asset_id> <comment_id> --note "addressed in v2"
```

The agent then **regenerates the report** — re-publishing the same `asset_id` (which **clears** the prior comments — a revision is a fresh document) or publishing a new one — and the operator re-comments if needed. There is no Approve step, and no review status the operator manages. Closing a report's tab **deletes** the asset and its comments (and closes any pop-out of it); closing the chat never deletes an asset.

## Spec-Anchored Reports

Pass `--spec-id <spec_id>` when the report is durable work-product for a spec rather than a session-only exchange. Spec-anchored assets remain discoverable from any chat carrying that spec id:

```bash
agent-orch asset list --spec-id <spec_id>
```

Session-scoped assets stay tied to the producing stream. Use session scope for ordinary review loops with one live agent; use spec scope for artifacts that another future agent must find after the producer closes.
