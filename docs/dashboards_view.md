# Dashboards view

Dashboards presents one ordered list with two kinds of entry:

- **Built-in:** code rendered inside Pentacle, including generic reports and
  trusted in-page web adapters.
- **Hosted:** an HTTPS web app served by a fleet machine. Web opens it in the
  dashboard panel; mobile hands it to the system browser.

The catalog alone controls membership, order and visibility. The renderer walks
`boards[]` in array order and omits entries with `visible:false` (default: true).
Registered code resolves built-in IDs; it does not add, prepend or sort list
items. A missing implementation stays unavailable in its catalog position.
There is no separate retired group or profile visibility override. Chat and room
controls are unchanged; `panel-dashboards`, `dashboard-list` and
`dashboard-content` remain stable DOM IDs.

## Local configuration

Use the existing exported configuration object in a private file selected by
`PENTACLE_CONFIG=/absolute/path/pentacle.config.local.js`; the loader does not
merge an overlay with the example configuration. See
[Desktop configuration](desktop_config.md). Restart the desktop, or restart the
web host and reload its page, after profile changes.

| Key | Effect |
| --- | --- |
| `features.dashboards` | Enables the Dashboards switcher |
| `dashboards.catalogSpecId` | Locates the catalog; set `pentacle__dashboard_catalog` for the hosted-record commands |
| `dashboards.catalogRoot` | Web-host directory of immutable adapter versions; never sent to the renderer |

The profile locates the catalog; `dashboards.hidden`, `dashboards.showRetired`
and `dashboards.modeler3d.url` no longer control the list or hosted URL. Modeler
is a normal hosted catalog record, with no separate built-in registration.
Without a configured catalog or usable cache, there is no independently ordered
built-in fallback; an empty list reads “No dashboards configured”.

## Hosted URL and web admission

The daemon service owns these runtime inputs:

- `PENTACLE_HOSTED_DASHBOARD_TAILNET_SUFFIX`: permitted fleet DNS suffix.
- `PENTACLE_HOSTED_DASHBOARD_PENTACLE_ORIGIN`: canonical HTTPS Pentacle origin,
  matching the web server's configured origin.

Clients receive the nonsecret `{tailnetSuffix, pentacleOrigin}` policy through
`hostedDashboardPolicy` on the authenticated daemon connection. Policy is never
accepted from catalog content or persisted with cached entries. Missing or
invalid policy refuses hosted writes and opening.

Admission requires an absolute, credential-free HTTPS URL on a subdomain of the
configured suffix at a DNS-label boundary. Userinfo, query strings, fragments
(including empty `?` or `#`), whitespace and backslashes are refused. Pentacle's
own hostname is refused even on another port: cookies are not port-scoped.
Commands and full-catalog publication enforce this rule. Clients recheck it
immediately before frame navigation or external opening, including cached URLs.
For example, with synthetic suffix `example-tailnet.ts.net` and Pentacle origin
`https://console.example-tailnet.ts.net`, a record may use
`https://viewer.example-tailnet.ts.net:8444/demo/`.

**Hosted web opening requires identity mode.** The web server derives
`hostedDashboardAuthMode` from its effective authentication object: `tailscale`
becomes `identity`, `token` becomes `token`, and missing or unrecognized modes
become `unknown`. It exposes that value in `window.__PENTACLE_CONFIG__` and
`/api/config`; profile, catalog, daemon and storage values cannot override it.

Before every mount, Reload/Retry or external-open action, the renderer fetches
same-origin `/api/config` with caching disabled and requires explicit `identity`
as well as current URL admission. Token/unknown modes, pending resolution,
malformed responses and failed requests keep hosted entries in place but
unavailable. They assign no iframe URL and invoke no opener. Cached identity-era
catalogs and restored selections do not authorize opening. Connection/config
invalidation removes the frame and discards the admission decision; an obsolete
asynchronous response cannot reopen it. Built-in behavior is unchanged.

Token-mode hosted opening is excluded. Enabling it requires exact
canonical-Origin enforcement on the `/cc` bridge **before dispatch**, rejecting
foreign, `null` and missing Origin, plus real-browser credential and authority
negative tests. The token cookie check alone is insufficient; token-mode refusal
is not proof of token-mode isolation.

## Hosted states and isolation

The common dashboard panel shows the selected title, loading and unavailable or
error states. Hosted navigation has a 15-second timeout; failure offers “Could
not open dashboard” and Retry. Reload removes the old frame and timer and runs
admission again. Open in new window also runs admission before opening. Leaving
the view removes the frame, handlers and timeout; late events cannot update a
new view. A frame load means navigation completed, not that the app rendered,
authenticated or passed a health check. There is no hosted health polling.

Hosted frames use `sandbox="allow-scripts allow-same-origin"`,
`referrerpolicy="no-referrer"`, `allow="xr-spatial-tracking; fullscreen"` and
`loading="lazy"` on a distinct permitted host. Popup and top-navigation sandbox
permissions are absent. External web opening uses `noopener,noreferrer`.
Pentacle supplies no tokens, auth headers, session/config injection or action
bridge to the hosted page. The hosted app may have its own unrelated session.

Keep application hosting tailnet-only. Deployment validation must prove that
Pentacle cookies/storage and authority do not reach the hosted app: a broadly
scoped Pentacle cookie reaching it fails the isolation requirement. Real-browser
checks must exercise parent DOM/storage reads, action-request `postMessage`,
top-navigation/opener escape and foreign/null/missing-Origin `/cc` attempts,
asserting refusal and no host action. Record credential-presence booleans, never
secrets. A synthetic frame load or configuration readback does not prove this
boundary. Hosted apps fetch their own assets; no client health probe or CORS
change is required.

## Mobile

Mobile uses the same catalog order and visibility. Hosted selection and explicit
Open/Retry validate the current daemon URL policy before handing the URL to the
system browser, including cached entries, with no Pentacle session or headers
attached. The browser owns loading and unreachable-page presentation. Returning
to Pentacle does not automatically reopen the URL. Native WebView embedding is
not used. Existing supported built-ins and reports keep their native behavior;
unsupported web adapters remain unsupported.

## Runtime catalog

Personal boards arrive as the `dashboard-catalog` asset and, for trusted web
adapters, immutable files installed on the web host. The sole catalog used by
hosted-record commands is `(spec_id=pentacle__dashboard_catalog,
asset_id=dashboard-catalog)`. Configure each client's catalog locator to that
spec. Other configured spec IDs are still readable, but the commands do not
modify them.

Entering Dashboards fetches and validates the whole catalog. Re-entering fetches
again; selecting a board does not refetch, and there is no catalog polling loop.
Changed catalog content needs neither a host restart nor a renderer rebuild.

**Schema.** `services/_shared/asset_schema.py` validates bounded JSON with
`schema_version:1`, `catalog_version`, `package`, `requires.host_api`, up to eight
`libs` and up to 64 `boards`. The mixed-list extension uses `requires.host_api:2`;
API-1 catalogs remain readable. Unknown keys and duplicate IDs reject the whole
catalog. Every board has `id`, `name`, `kind`, optional `description` and optional
boolean `visible`. Descriptor kinds are:

- `built-in`: resolves its ID to registered code.
- `report` and `web-adapter`: retain their existing fields and render as built-in.
- `hosted-view`: supplies `hosted.url`, without adapter code or actions.

Array position supplies order. File paths remain relative `web/<name>.js|css`
with a sha256; `hosted.url` is the only URL field. Shared synthetic cases live in
`test/fixtures/dashboard_catalog/catalog_cases.json`. A minimal hosted record is:

```json
{"id":"example-view","name":"Example view","kind":"hosted-view","hosted":{"url":"https://viewer.example-tailnet.ts.net:8444/demo/"}}
```

**Writers.** Only a server-verified live internal seat on a configured fleet host
may mutate hosted records, including records served by another fleet host.
Anonymous, operator-only, report-producer, external and scoped client principals
are refused; a claimed host string does not establish identity. The same rule
applies to direct catalog publication and deletion, including attempts to change
its content type. This does not widen trusted executable-adapter publication
authority. Hosted-record commands cannot upload or authorize adapter code.

See [Dashboard commands](../services/agent-orch/README.md#dashboard-commands) for
add, replacement and removal syntax. Full-catalog editing controls built-in
membership/order/visibility and can restore `visible:true` on a hidden entry.
An eligible seat publishes a complete validated catalog with:

```sh
agent-orch asset publish --type dashboard-catalog --title "Dashboard catalog" \
  --content-file /path/to/catalog.json \
  --asset-id dashboard-catalog --spec-id pentacle__dashboard_catalog
```

**Retrieval.** Discovery makes `asset.list { spec_id: catalogSpecId }` and selects
only the row whose `asset_id` is exactly `dashboard-catalog` and whose
`content_type` is `dashboard-catalog`; similarly named assets are ignored.
Metadata includes the owner `stream_id` and the `producer`; the client then calls
`asset.get { stream_id: <listed stream_id>, asset_id, spec_id }` (the existing
operator-authenticated path). Unknown keys, invalid bounds, paths, hashes,
URLs, descriptors or duplicate ids reject the whole catalog. `requires.host_api`
above the client's `HOST_API = 2` is unsupported rather than malformed.

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

A hosted view uses the generic hosted panel with its catalog ID, name and URL,
subject to [URL and web admission](#hosted-url-and-web-admission). It has no
trusted-adapter mount context or Pentacle actions.

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

**Release.** Ship compatible readers before an API-2 catalog. Install each
immutable adapter version directory and verify its hashes and authenticated
serving route before publishing the catalog. Before a complete package publish,
read the latest catalog and carry forward hosted additions, array order and
visibility; retain the complete preimage and verify the resulting union. Use the
existing owner-coordinated serial publication path. A hosted-record edit leaves
`package`, `catalog_version`, libraries and unrelated records unchanged; it does
not create a new immutable adapter package. Coordinate catalog/client/profile
rollback using retained preimages and installed version directories. Unsetting
`catalogSpecId` disables catalog retrieval, not a switch to a separate list.

## Validation

Run `npm run prestart && npm test`, `npm run build:web`, and
`node test/e2e/web_gate.js` from the repository root. Catalog, visibility, hosted
panel and auth tests cover the shared fixtures, mixed ordering, URL policy and
server-derived mode. Browser coverage must include fresh and cached catalogs:
identity opens permitted URLs; token/unknown mode produces no hosted navigation,
iframe URL assignment or opener call. Include identity-to-token/unknown
reload/reconnect and invalidated asynchronous decisions.

The `dashboard_catalog` scenario exercises catalog discovery, report retrieval,
versioned adapter files/SRI, hosted navigation and preserved chat state. Adapter
unit tests use synthetic frame events and timers; they do not establish browser
isolation or deployed app rendering. Use the hermetic gate, never production,
for synthetic fixtures; report browser startup failures as NOT RUN.

For private-package gate fixtures only, `PENTACLE_TEST_CATALOG_ROOT` supplies
version directories whose `catalog.json` files are published verbatim.
`PENTACLE_TEST_CATALOG_REPORT_ROWS` optionally supplies synthetic report assets
as `[{ "spec_id", "producer", "asset_ids": [...] }]`. These affect only the gate's
scratch profile, never host defaults; the fixture spec is
`example__dashboard_catalog`.

List items expose `data-dashboard-id`; `#dashboard-content` exposes
`data-catalog-version`. Board state uses `data-board-state`, including
`loading`, `ready`, `empty`, `partial`, `error`, `unsupported` and `unavailable`.
Catalog/report cards retain the `dashboard-catalog-error`,
`dashboard-catalog-unavailable`, `dashboard-board-error`,
`dashboard-board-unsupported`, `dashboard-report-latest`, `dashboard-report-list`,
`dashboard-report-truncated` and `dashboard-report-empty` test IDs.
