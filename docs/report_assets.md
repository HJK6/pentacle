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

## Report-Producer Principal

One fixed service principal may publish one kind of report asset without a seat. Its name and binding are runtime configuration, not source: the daemon's `PENTACLE_REPORT_PRODUCER_CONFIG` holds only the path of a user-owned mode-0600 JSON file (never a token). Unset, unreadable, foreign-owned, group/world-accessible or invalid means the principal is disabled; the file is re-read on every RPC, so removing or editing it takes effect on the next call.

```json
{"stream_id": "examplehost:daily-report",
 "token_file": "/private/local/daily-report-token",
 "spec_id": "spec_example__daily_reports",
 "asset_id_pattern": "^daily-report-([0-9]{8})(?:-r[0-9]+)?$",
 "cutoff_format": "%Y%m%d",
 "title_template": "Daily report {cutoff}",
 "tag": "daily-report",
 "body_max_bytes": 65536}
```

- `stream_id` is `host:session`, also the fixed storage anchor (no seat is created), and may not be the CD or WMI backup principal. `token_file` is an absolute path to its own user-owned 0600 token file; a value equal to the CD or WMI backup credential is refused.
- `asset_id_pattern` has exactly one group, the cutoff. `cutoff_format` (optional) must turn a sample date into text that parses back to the same calendar date (literal suffixes such as `T1300Z` are fine; `%Y` or `%m%d` alone are not). `title_template` contains `{cutoff}` once; `body_max_bytes` is at most 1 MiB.
- The client connects with an RPC hello (`from_stream_id` = the principal, `stream_token` = the file's token, `subscribe: {snapshot: false, mode: "rpc"}`) and may then send only `asset.publish`; every other verb, including one the daemon does not implement, is `system_producer_forbidden`. The publish must carry `from_stream_id`, `stream_id` and `producer` equal to the principal, type `report`, the configured spec id, exactly the configured tag, an asset id matching the pattern with a valid cutoff, the title for that cutoff and a body within the cap; anything else is `system_producer_payload_invalid` with no write.
- A published asset id is immutable. Republishing the identical title and body returns `asset.publish.ok` with `unchanged: true` and no write or broadcast; any other content for that id, including a malformed body, is `report_producer_immutable`. A correction is a new id (for example a `-rN` suffix).
- No other caller may claim the principal's producer id, or publish an id matching its pattern under its spec, first or later (`asset_unauthorized`). The check and the write are one store operation, so concurrent publishes cannot interleave. `asset.list` metadata carries `producer`, so readers can trust only records produced by the configured principal.
