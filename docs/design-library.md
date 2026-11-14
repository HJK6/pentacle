# Design library

The Mockups dashboard and `/designs/` show live HTML/UI designs from a private,
local design library. Views with prepared documents appear; screenshots and
recorded videos remain archived assets and are never viewer content. A live view
need not contain clickable interactions. Mobile previews use their recorded phone
viewport and fit narrower containers while preserving its proportions.

Prototype opens the selected live design by default. Flow overview renders the
workspace's views together in a horizontal scrolling region at their declared
viewport sizes. Both use the same opaque sandbox and authenticated document API.
Use these existing per-view fields to publish a workspace:

- `group`: shared workspace label.
- `variant: "prototype"`: the navigable prototype entry.
- `variant: "flow-01"`, `"flow-02"`, etc.: ordered live overview entries. The
  positive numeric suffix determines order; equal numbers sort by view ID.
- `title`: view label; `surface`: `mobile` or `web`; `viewport`: width/height in
  CSS pixels. Each entry has its own prepared HTML document.

Multiple prototypes per project or workspace are supported. Untagged prepared
views remain selectable. A workspace with only flow entries uses its first flow
view as the default; when no flow entries exist, its live views form the overview.
Grouping never crosses project or revision boundaries. Polling preserves running
frames; switching selection, mode or revision disposes the previous previews.

Claude Design remains the design source of truth until a separate parity
cutover. Importing a page never approves it. The library contains mirrors and
local candidates; approval labels require an explicit evidence pointer.

Set `dashboards.designLibraryRoot` in the web host's private profile, or set
`PENTACLE_DESIGN_LIBRARY_ROOT`. This directory is never a public Git payload.
The setting stays server-side. The existing token-cookie or Tailscale proxy
authentication applies to every gallery, catalog, document and media request.

## Format and import

Supply a directory containing `drop.json` and `payload/`, or a ZIP containing
that same layout. `drop.json` uses schema `design-drop.v1`:

```json
{
  "schema": "design-drop.v1",
  "project_id": "example",
  "title": "Example",
  "source_kind": "local",
  "source_id": "example-design",
  "source_revision": "revision-1",
  "exported_at": "2026-01-01T00:00:00Z",
  "exporter": "project owner",
  "complete": true,
  "missing_paths": [],
  "files": [{"path":"index.html","sha256":"<64 lowercase hex>","size":123}],
  "views": [{"id":"home","title":"Home","entry":"index.html","surface":"web","viewport":{"width":1440,"height":900},"approval_ref":null}],
  "flows": []
}
```

`source_kind` is `local`, `claude-design` or `claude-artifact`. Supply
`source_id` and/or `source_url`; use explicit `unknown` when the source revision
is unavailable. Preserve source curation documents, archive/duplicate labels
and original inventory. `group`, `variant`, `ready_selector` and an optional
`fallback` image path can be supplied per view. A fallback describes an actual
retained reference; do not substitute an unrelated image.

Flows have a stable `id`, `title`, `entry`, `view_ids`, `viewport` and explicit
ordered `steps`. Each step has a `name` and `action`: `click`, `fill`, `assert`
or `wait`. Browser selectors are Playwright selectors. Assert steps wait for
visibility and optionally check `text` or `attribute`/`value`; fill requires
`value`; waits specify `ms` up to 5000. Include state assertions after navigation
and name every state to capture. Flows are curated, never inferred from filenames.

Unknown schemas, traversal, absolute/backslash paths, symlinks, duplicate or
case-colliding paths, unlisted files, digest/size mismatches and unsupported
source file types fail import. Limits are 10,000 files, 512 MiB expanded total,
64 MiB per source file and 32 MiB per prepared document. Source JavaScript and
JSX are inert originals until the curated preparation/capture process; exported
instructions, package scripts and editor runtimes are never executed.

## Prepare and publish

Install the repository development dependencies and Playwright's recording
helper (`npx playwright install ffmpeg`). Chromium/Chrome and system `ffmpeg` /
`ffprobe` must be installed. `PENTACLE_CHROME`, `PENTACLE_FFMPEG`,
`PENTACLE_FFPROBE` and `PENTACLE_PYTHON` can name the provisioned executables.
The browser uses its sandbox; there are no certificate or harness bypass flags.

Use an owned staging directory **outside** the synchronized library:

```sh
node scripts/design-library.cjs publish \
  --drop /private/design-drop --staging /private/design-staging \
  --library /private/design-library --dependencies /private/dependencies.json \
  --out /private/design-evidence
```

`publish` imports immutable source, prepares documents, captures views, records
flows, verifies content hashes, installs an immutable revision and atomically
selects it last. Failed flows preserve current. Incomplete drops remain indexed
drafts and never select current. An identical input with verified retained
output is reused. Tool/dependency/output changes produce a new revision.

For diagnosis, the same CLI exposes `import --drop --staging [--dependencies]`,
`prepare --dir`, `record --dir [--out]` and `verify --dir`. `record --library
--project --revision --out` re-records a retained prepared revision into a new
owned derivative directory; it does not rewrite or select a published revision.
The library defaults to `PENTACLE_DESIGN_LIBRARY_ROOT` or
`~/agent-workspace/design-library`. `verify --library --project` checks
the entire selected revision. Omitting project returns the verified catalog.

Preparation performs no downloads. A private dependency profile supplies
`assets`, keyed by exact public URL. Each asset requires a local `path`,
`sha256`, `version`, `license` and `content_type`; reviewed React/ReactDOM UMD
assets additionally set `trusted_script: true`. Fetch exact public dependencies
once during authorized preparation, record their bytes/licenses, then prepare
offline. CSS font/image references must also be pinned. Babel's known standalone
runtime is removed; esbuild compiles JSX before viewing.

Optional `adapters` replace a specific source script's bytes with a reviewed
deterministic derivative. Each names `path`, original `sha256`, `replacement`,
`reason`, `review_ref` and `mocked`. A stale hash refuses the adapter. Originals
stay untouched. Saved canvas state can be embedded; persistence/export operations
remain unsupported and labelled. Unmocked Claude calls and unsupported network,
navigation or dynamic loading operations refuse interactive preparation.

Each prepared document has restrictive CSP and fresh in-memory local/session
storage implementing `getItem`, `setItem`, `removeItem`, `clear`, `key`, `length`
and string coercion. The parent obtains it through authenticated JSON and uses
`iframe.srcdoc`, `sandbox="allow-scripts"` and `referrerpolicy="no-referrer"`.
There is no same-origin privilege, parent bridge, persistent editor or Claude
shim. Prototype messages are ignored. Storage resets on reload. This curated
preparation plus actual-browser zero-request proof is the offline boundary;
an opaque iframe alone is not an OS network airgap for arbitrary JavaScript.

Compatibility is per view: `offline-interactive`,
`offline-with-mocks/limitations`, `screenshot/video-only` or `incomplete`, with
an exact reason and available fallback. Browser capture waits for named ready
conditions, fonts and decoded images. Flows retain WebM plus H.264/yuv420p MP4
with faststart, named state PNGs, browser/tool/viewport/flow identities and
ffprobe metadata. State assertions and distinct sampled video frames are
required; nonzero bytes alone do not pass.

## Editing, sync and rollback

```text
projects/<project>/revisions/<revision>/
  source/ prepared/ screenshots/ videos/ manifest.json
projects/<project>/current.json
projects/<project>/history/<pointer-preimage>.json
```

`manifest.json` uses `design-library.v1`. `input_id` identifies normalized source
inventory, view/flow definitions and preparation profile. `revision_id` hashes
the final canonical source/prepared/generated inventory and metadata, excluding
its own identity/import timestamps and generated self-references. Generated
bindings identify their containing revision. Files and outputs are hash-checked
before exposure; raw HTML/JS and arbitrary paths have no public file route.

Keep one project writer. On each execution host, copy source into an unsynced
task workspace, make only that copy writable, edit there, reindex the drop's file hashes/sizes and send the
candidate to that writer through existing authenticated SSH. The writer runs
the full publish operation. Do not edit a synchronized immutable revision.

Reuse a dedicated Syncthing folder for this library: publisher send-only,
reader peers receive-only. Install immutable bytes first and `current.json`
last. Every web process verifies the selected manifest and all its hashes,
retaining its previous verified revision while a newer selection arrives.
Temporary/conflict files are ignored and conflicts are reported, never resolved
by automatically picking a copy. A cold process has no unverified fallback.

After publication, run `verify --library --project` on every connected host and
compare selected revision hashes. Healthy peers target convergence within two
minutes. An offline peer is explicitly pending; local verification does not
claim fleet availability. Preserve native Syncthing config preimages for folder
changes; rollback removes only the newly added folder configuration.

To restore an older verified complete revision:

```sh
node scripts/design-library.cjs rollback --library /private/design-library \
  --project example --revision <retained-revision-id>
```

The command preserves the previous pointer and atomically replaces current.
Keep failed revisions/evidence. A web build/config rollback is separate from a
library pointer rollback; verify the actual authenticated browser journey after
both. Deploying the chat daemon does not activate a new web build.

## HTTP and checks

`GET /api/designs` returns the catalog; `GET /api/designs/<project>/<revision>`
returns retained revision metadata; `GET /api/designs/<project>/<revision>/<item>`
returns item metadata and its prepared document when available. `GET`/`HEAD`
`/design-media/<project>/<revision>/<asset>` serves only manifest-allowlisted
PNG/JPEG/WebP/MP4 bytes with Range support. Authentication precedes all routes;
invalid IDs, hash mismatches, symlinks and unavailable revisions refuse serving.
Media uses bounded file streams for ranges; HEAD returns metadata without a
media body. Cold integrity checks hash in 64 KiB chunks. Verified immutable
revisions are reused only while canonical file identities, sizes and timestamps
match; canonical changes force verification again. Nested Syncthing temporary
and conflict copies never become canonical members, and conflicts remain visible.
Source-relative Copy path is an explicit agent action, never navigation.

```sh
node --test test/design_library.test.js test/design_library_http.test.js test/ui_review_artifacts.test.js
node test/e2e/design_library_probe.cjs --drop /private/design-drop \
  --dependencies /private/dependencies.json --out /private/probe-evidence
node test/e2e/web_gate.js --scenario design-gallery
npm --prefix vendor/dashboard-library run build
npm --prefix vendor/dashboard-library run check:dist
npm --prefix vendor/dashboard-library test
npm run build:web
```

Set `PENTACLE_TEST_DESIGN_DROP` to include a real private drop in the unit import
check. The named web scenario covers 1440×900, 390×844 and 375×812, project/flow
selection, media seeking, reload preservation, opaque sandbox and cleanup.
Real-export capture/record evidence remains private. Mobile WebView integration,
iOS Safari and phone-network auth belong to the separate mobile delivery.
