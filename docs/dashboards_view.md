# Dashboards view

The web and Electron renderer share the registry, list and adapter lifecycle.
The list uses cosmic tokens, ACTIVE and optional RETIRED groups, accent dots,
descriptions and keyboard-native selection buttons. Chat and room controls are
unchanged. `panel-dashboards`, `dashboard-list` and `dashboard-content` remain
stable DOM IDs.

## Local configuration

Add these keys to the existing exported configuration object. Keep its other
settings: the loader does not merge an overlay with the example configuration.
Select a private JavaScript configuration file outside the repository through
`PENTACLE_CONFIG=/absolute/path/pentacle.config.local.js`; that filename is not
automatically discovered. See [Desktop configuration](desktop_config.md).
Restart the desktop, or restart the web host and reload its page, after edits.

| Key | Default | Effect |
| --- | --- | --- |
| `features.dashboards` | Existing feature setting | Boolean that exposes the Dashboards switcher |
| `dashboards.hidden` | `[]` | Exact dashboard IDs to hide, including otherwise opted-in retired boards |
| `dashboards.showRetired` | `false` | Only boolean `true` lists registered retired boards |
| `dashboards.modeler3d.url` | Absent | Absolute HTTP(S) viewer URL without embedded username/password |
| `dashboards.catalogSpecId` | Absent | Spec containing the runtime `dashboard-catalog` asset; use local configuration only |

The URL has no built-in host or default. Do not put real URLs, credentials,
private hostnames or captured models in Git. The normal renderer config path
loads this field: desktop `config-loader` / `getConfig`, or the web host's
injected public config and the same `getConfig` bridge. No new config service
or viewer proxy is involved. Keep credentials out of the URL; the configured
viewer must use its existing browser authentication.

Retired adapters keep registering under their existing conditions. The registry
filters explicit hidden IDs, then hides retired adapters unless opted in. It
selects the first visible active board, or the first visible retired board if
that is all the opt-in leaves. An empty list reads “No dashboards configured”.
The 3D Modeler stays listed when unconfigured unless explicitly hidden.

## 3D Modeler states and controls

- **Unconfigured:** no usable URL; the setup key is shown and no frame or timer is created
- **Loading:** a fresh sandboxed iframe is navigating; the load timeout is 15 seconds
- **Loaded:** the iframe's navigation completed. This does **not** prove that a model rendered, authentication succeeded or framing was allowed. Browsers can fire `load` for error or refused documents
- **Blocked:** an iframe error event or timeout occurred. The reason suggests possible causes without claiming to diagnose inaccessible cross-origin headers

Open in new window and Reload remain visible in every state. The link is
inactive without a valid URL. Electron uses the existing `openExternal` bridge;
the web opens the configured viewer in a new tab/window. A reported bridge
failure appears inline. Reload discards the previous frame and timer. Leaving
Dashboards removes the frame, its handlers and timeout; late events cannot
change the next view. The adapter does not poll or fetch viewer headers.

## Viewer compatibility and isolation

The synthetic adapter uses `sandbox="allow-scripts allow-same-origin"` and
`referrerpolicy="no-referrer"`, `allow="xr-spatial-tracking; fullscreen"` and
`loading="lazy"`. Only configure a trusted viewer. In particular,
a same-origin viewer with both sandbox permissions is not a strong isolation
boundary from the parent application. Popups, top navigation and other sandbox
permissions are not enabled by this adapter.

The fleet's compatibility errata reports that the viewer is embeddable as served:
no X-Frame-Options, Content-Security-Policy, CORS or WWW-Authenticate headers;
no frame-busting or parent/postMessage use. Access depends on network
reachability. This is a fleet-supplied fact, not a probe performed by this adapter.
The frame fetches its own same-origin assets. Pentacle never fetches those assets
or needs their CORS permission.

No Content-Security-Policy is added by this packet. If a CSP is ever introduced,
`frame-src` must include the viewer's exact origin with its port.
The fleet must verify real rendering with its browser journey against the served
app before operator acceptance. The synthetic gate and a “Loaded” label do not
substitute for that check. Real model files and their licences stay outside the
public source tree.

## Runtime catalog

Personal boards are not part of this repository. An operator delivers their own
boards at runtime as one `dashboard-catalog` asset, published to the chat-stream
daemon under a spec id of their choosing, plus (for web adapters) an immutable
directory of files on the web host. With no `dashboards.catalogSpecId` the view
shows only the existing `shared-demo` and `modeler-3d` built-ins and makes no
catalog call. Hiding all visible boards through `dashboards.hidden` shows “No
dashboards configured”; the normal default does not remove those built-ins.

The loader runs after boot in its own error boundary. Entering Dashboards fetches
once, validates the whole catalog and merges its boards after built-ins in array
order. Leaving and re-entering fetches again; there is no catalog polling loop.
Selecting a different board does not refetch the catalog. A catalog id colliding
with a built-in produces a board-only error and does not replace that built-in.
A changed local config setting requires the normal restart/reload, but changed
catalog content needs neither a host restart nor a renderer rebuild.

**Catalog asset.** `asset_id` is exactly `dashboard-catalog`, `content_type`
`dashboard-catalog`, published from an operator seat:

```
agent-orch asset publish --type dashboard-catalog --title "Dashboard catalog" \
  --content-file <catalogRoot>/<catalog_version>/catalog.json \
  --asset-id dashboard-catalog --spec-id <your catalog spec id>
```

The daemon and the CLI validate it with `validate_dashboard_catalog` in
`services/_shared/asset_schema.py`, the authority for the schema: bounded JSON
data (`schema_version` 1, `catalog_version`, `package`, `requires.host_api`,
`libs` ≤ 8, `boards` ≤ 64 of kind `report`, `web-adapter` or `hosted-view`),
unknown keys refused at every level, duplicate board ids refuse the whole
catalog, file paths are relative `web/<name>.js|css` with a sha256, and the only
URL is `hosted.url`. Board order is the catalog's array order. Synthetic accept
and refuse cases shared by the daemon and the clients are in
`test/fixtures/dashboard_catalog/catalog_cases.json`.

**Retrieval.** Discovery makes `asset.list { spec_id: catalogSpecId }` and selects
only the row whose `asset_id` is exactly `dashboard-catalog` and whose
`content_type` is `dashboard-catalog`; similarly named assets are ignored.
Metadata includes the owner `stream_id` and the `producer`; the client then calls
`asset.get { stream_id: <listed stream_id>, asset_id, spec_id }` (the existing
operator-authenticated path). Unknown keys, invalid bounds, paths, hashes,
URLs, descriptors or duplicate ids reject the whole catalog. `requires.host_api`
above the client's `HOST_API = 1` is unsupported rather than malformed.

**Cache and failures.** The last validated catalog is cached per spec in memory
and `sessionStorage`. On transport/unavailable failure, cached boards may render
with both “Dashboard catalog unavailable: <reason>” and “catalog cached <age>”.
Without a good cache there are no catalog boards. Malformed or unsupported data
shows “Dashboard catalog unsupported/malformed: <reason> (catalog
<version|unknown>)”; it does not render the cached boards or evict the good cache.
A valid fetch replaces the cache atomically. Board failures show “Board failed
to load: <reason>” without breaking chat, the other boards, or app startup.

**Generic report boards.** A `report` board reads its namespace with one
bounded request `{ spec_id, asset_id_prefix, producer, sort: "asset_id_desc",
limit: W }`: the daemon applies the literal prefix and exact producer filters
and the byte-wise id sort before the limit, reading an index range so the scan
stops at `W` rows; a window needs `sort` and `limit` 1–400 and a spec-scoped
list. These are reader filters, not authorization. The id grammar is `^<prefix><key_format>(-r<rev_width digits>)?$`
with all-zero revisions excluded (`report_id_grammar`); under it, byte order is
(key, revision) order. Synthetic retrieval fixtures F-A–F-E (foreign rows,
revisions, `-r09`/`-r10`, `-r9`/`-r00` errors, full window, empty) are in
`test/fixtures/dashboard_catalog/report_retrieval_cases.json`.

For each mount or explicit refresh, `W = min(4 * history_limit, 400)` and there
is exactly one list call, no pagination, and no report polling loop. An id in the
namespace that fails the grammar produces “unsupported asset id <id> in namespace
<prefix>” before any latest body is fetched. Zero rows produce “No <name> yet
(last checked <time>)”. A shorter-than-W result is complete; a full W-row window
is partial. The first returned raw id is latest, never a client sort by
`updated_at`. The history list holds up to `history_limit` distinct keys at their
highest revision. A partial result labels that first row “latest” only when
`writer_enforced` is true; otherwise “latest in loaded window”. If the full window
contains fewer distinct keys than requested, show “history truncated: K of up to
N loaded”. Titles substitute only `{key}` and `{rev}`. The selected body is fetched
with the metadata's `stream_id` and rendered by the existing generic `renderAsset`.

**Web adapters and hosted views.** Web adapters load libraries in catalog order,
then the board's CSS, then its script. Every tag's URL is exactly
`/dashboards/private/<catalog_version>/<path>` using the fetched version; every
tag carries `integrity="sha256-<base64 of the 32-byte digest>"` and
`crossorigin="anonymous"`. Loading waits for each tag's load/error event. The
script must register the same declared id in `window.DASHBOARDS`; missing/wrong
registration, load failures and SRI failures produce a board error. A listed
file's 404 is reported as “catalog files for version <X> not installed”.

A hosted view uses the existing configured-iframe implementation with its declared
id, name and URL. The sandbox and 15-second load timeout, Reload and Open in new
window controls are unchanged. A successful frame navigation does not verify
cross-origin page contents. Catalog settings do not reconfigure the built-in
`modeler-3d` board.

**Private files (web host).** `dashboards.catalogRoot` names a directory of
version directories `<catalogRoot>/<catalog_version>/` (each with its own
`catalog.json` and `web/` files, never edited after creation). The host serves
`/dashboards/private/<catalog_version>/<path>` only after authentication, only
for paths listed in that version's `catalog.json`, only beneath the version
directory's realpath, and only when the bytes match the listed sha256;
everything else is `404`, with `cache-control: no-store`. A new version
directory is served without a host restart, side by side with older ones.
`catalogRoot` never reaches the renderer.

**Host adapter context.** The shell mounts an adapter with
`{ config, household, assistant, actions }`. Its registered `actions` metadata
must come from the catalog loader's validated descriptor; adapter-authored
claims are not authority. The mount-context seam reads that metadata, while
catalog loading and validation remain the loader's responsibility. Missing or
malformed metadata grants no actions.

- `household` is `{ selectors, store }` only with the `household` capability,
  otherwise `undefined`. The host configures the shared in-memory store once at
  startup. Adapters use its read/mutation methods and never reconfigure it; no
  raw command callback is exposed in the context. Reads use `load(month)` and
  `refresh()`; mutations use `addItem`, `checkItem`, `removeItem`, `addEvent` and
  `removeEvent`, preserving the shared undo/readback/month-lock behavior.
- `assistant` is provided only with `assistantState`. It is resolved once per
  mount from the protected assistant session as `{ name, hostId, sigilMarkup }`,
  or `null` when there is no such session. The callable
  `sigilMarkup(label, size)` returns the host's assistant sigil HTML. Without
  the capability, `assistant` is `undefined`.
- `actions` always contains exactly the async `assetList(params)` and
  `assetGet(params)` functions. An allowed call returns the existing bridge's
  daemon reply and preserves rejection behavior. A disallowed call logs a
  warning and resolves `{ ok: false, error: 'action_not_allowed' }` without
  contacting the bridge. An unavailable asset bridge method is denied the same
  way. `household` and `assistantState` are context gates, not additional callable
  members of `actions`.

Mounting itself does not load household data; the shell's initial poll owns
that read. The context seam does not load or register a private adapter by
itself, and it does not replace catalog validation. When `assistant` is `null`,
show no lamp and leave label text unchanged.

The allowlist is an API convention, not a sandbox. Catalog adapter scripts run in
the page and can access its existing globals/bridges; install only trusted code.
SRI verifies the pinned bytes, not whether that code is safe.

**Release.** Install the new version directory, verify every file's sha256
against its `catalog.json`, fetch one file through the route, then publish the
catalog asset; publishing is the only pointer switch. Roll back by republishing
the previous version's `catalog.json` (its directory stays installed). Unset
`catalogSpecId` and restart the host to turn the catalog off.

**Gate.** Scenario `dashboard_catalog` in `node test/e2e/web_gate.js` seeds only
synthetic catalogs (`0.2.0+aaaaaaa`, `0.2.1+bbbbbbb`) and report assets. A
pass-through bridge observer checks that the normal unset profile issues no
catalog list call from boot through view entry. A separately labelled synthetic
visibility configuration hides both built-ins to exercise the real empty state
without removing their registrations. Configured UI assertions cover:

- Catalog array order after built-ins and `data-catalog-version` on
  `#dashboard-content`; `catalogSpecId` reaches the renderer, `catalogRoot` does not
- Exact discovery/list/get via the listed owner; one filtered, sorted report
  window; `example-report-20261007T1300Z` latest, highest-revision history and the
  actual generic-rendered report body
- Library/CSS/script order, versioned URLs and SRI; `example-broken` receives
  HTTP-200 bytes that disagree with the published hash and must not execute
- `example-hosted` renders the real loopback iframe with the existing sandbox,
  navigation and controls; leaving the view removes it
- N+1 installed and published while the host runs, then rendered on view re-entry
  without rebuilding or reloading the document, while N files stay served
- `publishBody` supplies a daemon-valid `requires.host_api: 2` catalog and `corrupt`
  writes a malformed body past daemon validation; both show actual error cards
- Chat retains its nodes, stream, transcript and editable unsent draft after
  errors, hosted navigation and rollback; republishing N restores its adapter

List items expose `data-dashboard-id`. Board containers expose `data-board-state`
(`loading|ready|empty|partial|error|unsupported`), and catalog/report cards expose
the stable `dashboard-catalog-error`, `dashboard-catalog-unavailable`,
`dashboard-board-error`, `dashboard-board-unsupported`, `dashboard-report-latest`,
`dashboard-report-list`, `dashboard-report-truncated` and `dashboard-report-empty`
test ids as applicable. The unsupported-board card is for clients that do not
render a supported catalog kind; web renders all three kinds.

Test override for a private dashboard package's e2e (gate process only; it
changes the gate's scratch profile, never a host default):
`PENTACLE_TEST_CATALOG_ROOT=<dir of version dirs>` publishes each
`<version>/catalog.json` verbatim and serves those files;
`PENTACLE_TEST_CATALOG_REPORT_ROWS=<rows.json>` optionally seeds report assets
(`[{ "spec_id", "producer", "asset_ids": [...] }]`). The catalog spec id is
`example__dashboard_catalog`.

## Validation

Run `npm test`, `npm run build:web`, and `node test/e2e/web_gate.js` from the
repository root. `npm run prestart` builds renderer prerequisite bundles when
needed; the web build also ensures those prerequisites exist.

Catalog validation and retrieval coverage use `test/catalog_loader.test.js` and
`test/report_board.test.js` with every shared catalog/retrieval fixture.
Additional coverage is collected from `test/dashboards_visibility.test.js`,
`test/modeler_3d_dashboard.test.js`, and `test/dashboards_scenario.test.js`.
The existing four dashboard suites remain unchanged. Adapter tests use synthetic
frame events and deterministic timers; they do not establish browser behavior.

The web scenario `web-dashboards-revamp`, registers the real retired
adapter manifests under a scratch synthetic profile so absence is non-vacuous.
It checks the unconfigured modeler, rewrites only that hermetic profile with the
loopback viewer URL, restarts its test web host, and checks loaded navigation.
Each phase returns to Chats and checks the same chat nodes, stream, transcript
and unsent draft; the iframe is removed. The fixture server serves only
`test/fixtures/modeler_viewer.html`. No real models or external assets are used.
A screenshot is produced only by a successful real-browser run.

The scenario never mutates an external `--profile` configuration. For acceptance,
run the default hermetic gate, never against production. A local browser startup
failure must be reported as **NOT RUN**, with its failure output; unit and build
success do not certify the browser scenarios. The fleet runs that gate and CI
after applying the format-patch series when local browser execution is blocked.
