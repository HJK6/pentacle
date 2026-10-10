# Optional dashboards

Dashboard adapters register only when their existing configuration conditions permit it. Each adapter receives caller-supplied state and actions. Keep endpoints, credentials and captured records in local configuration, outside Git.

The bounded library in [`vendor/dashboard-library`](../../vendor/dashboard-library/README.md) provides six consumed boards: specs, foreclosure, chat-stream, ui-review, notifications and 0dte-trading. Business, mobile-testing, pi-control and scraper-bot remain self-contained. Loading the library exposes `PublicDashboardLibrary` and the legacy `TriforceDashboards` alias used by existing adapters; it does not register adapters in `window.DASHBOARDS`.

Definitions declare an ID and manifest plus either `render(state, ctx)` or `mount(container, ctx)`, `update(refs, state, ctx)` and `unmount(refs)`. The context carries `mode`, visibility checks and explicitly supplied action callbacks. Display mode hides interactive-only controls. Escaping and cleanup belong to the definition; a callback receipt and refreshed producer state determine write success.

Generate and verify the dependency with its `npm run build`, `npm run check:dist` and `npm test`. Do not edit generated files independently or silently load private producer modules. Missing shared definitions or writers must remain visibly unavailable.

Personal boards arrive at runtime through the operator's `dashboard-catalog` asset and the web host's authenticated `/dashboards/private/<catalog_version>/` route; see [Runtime catalog](../../docs/dashboards_view.md#runtime-catalog). Public source ships no personal board ids, endpoints or bundles.

## Visibility and selection

The catalog is the only list authority. `boards[]` supplies order and membership;
optional `visible:false` hides an entry. The renderer does not prepend registered
code, group by retirement status or apply `dashboards.hidden` /
`dashboards.showRetired`. Registration is an implementation lookup: a `built-in`
record resolves by ID, and a missing implementation gets an unavailable card in
its catalog position. Reports and trusted web adapters also present as built-in;
`hosted-view` is the other presentation kind.

Modeler uses the generic hosted implementation in `modeler-3d.js`; its membership
and URL come from a hosted catalog record, not a separate registration or
`dashboards.modeler3d.url`. See [Dashboards view](../../docs/dashboards_view.md)
for the public behavior and [hosted admission](../../docs/dashboards_view.md#hosted-url-and-web-admission)
for the URL, auth-mode and isolation requirements.

## Runtime catalog client

Set `dashboards.catalogSpecId` to `pentacle__dashboard_catalog` to read the sole
catalog managed by the [dashboard commands](../../services/agent-orch/README.md#dashboard-commands).
Keep endpoints, tokens and the catalog root in an operator-owned local profile,
outside public source. Restart/reload after changing that locator. Catalog content
is fetched on view entry after app boot; publishing changes needs no rebuild or
host restart. There is no catalog polling loop.

`catalog_loader.js` discovers the exact `dashboard-catalog` asset and content
type, retrieves it through its listed owner `stream_id`, and validates the whole
catalog. `HOST_API` is 2; API-1 catalogs remain readable. `merge` walks catalog
entries once in array order, filters hidden entries and resolves each renderer.
Duplicate IDs reject the catalog. With the locator unset there is no catalog
request or separately populated built-in list; no visible entries produces
“No dashboards configured”.

The last validated catalog is kept in memory and `sessionStorage`, scoped by
spec id. A transport/unavailable result may display it with a “catalog cached
<age>” badge and an unavailable card. An unsupported or malformed response shows
its error card and does not display stale catalog boards or evict the good
cache. Only a valid catalog atomically replaces it. Catalog failure is isolated
from startup, chat and other views.

- `report_board.js` makes exactly one bounded list request per mount/refresh,
  using the declared namespace, optional producer, descending raw asset id and
  `min(4 * history_limit, 400)` limit. It rejects an unexpected id before selecting
  a latest report, keeps the daemon's order, collapses history to the highest
  revision per key, and fetches the selected body through the listed stream.
  The existing generic `renderAsset` displays that body. A full window is partial;
  its latest label and optional truncation notice follow `writer_enforced` and
  the number of distinct keys. There is no report pagination or polling loop
- `web-adapter` loads each library, then CSS, then script from the fetched
  catalog's versioned host route, with `sha256-<base64 digest>` SRI and
  `crossorigin="anonymous"`. The script must register its declared id. Missing
  files, failed integrity, failed loading or incorrect registration produce a
  board error, while other boards remain selectable. No adapter ships here
- `built-in` resolves registered code by ID without altering catalog position
- `hosted-view` uses the generic sandboxed iframe lifecycle, 15-second timeout,
  Reload/Retry and external-open controls. Every opening first fetches uncached
  `/api/config`, requires server-derived `hostedDashboardAuthMode: "identity"`
  and validates the URL against current authenticated daemon policy. Token,
  unknown, failed config or missing policy stays unavailable, including cached
  entries. Connection/config invalidation removes the frame and prevents stale
  asynchronous admission from reopening it. Hosted pages receive no adapter
  context or Pentacle authority

The stable view hook is `#dashboard-content[data-catalog-version]`; board state
is `data-board-state` (`loading`, `ready`, `empty`, `partial`, `error`,
`unsupported`, or `unavailable`). List buttons use `data-dashboard-id`. Error/report elements use
`data-testid="dashboard-catalog-error"`, `dashboard-catalog-unavailable`,
`dashboard-board-unsupported`, `dashboard-board-error`, `dashboard-report-latest`,
`dashboard-report-list`, `dashboard-report-truncated`, and `dashboard-report-empty`
as applicable. The unsupported-board hook is for clients that cannot render a
descriptor kind; unavailable hosted entries keep their catalog position.

## Catalog mount context

The host mounts catalog adapters with `{ config, household, assistant, actions }`.
Only the loader's validated descriptor grants capabilities:

- `ctx.actions` always has exactly the async `assetList(params)` and
  `assetGet(params)` bridge wrappers. An action outside the descriptor's allowlist,
  or an unavailable bridge method, warns and resolves
  `{ ok: false, error: 'action_not_allowed' }`
- `ctx.household` is present only for `"household"` and is exactly
  `{ selectors, store }`. The host configures the shared store once. Adapters
  call `load(month)`, `refresh()`, `addItem`, `checkItem`, `removeItem`, `addEvent`,
  and `removeEvent` on that store; they never call `configure` or a raw command
- `ctx.assistant` is present only for `"assistantState"`, resolved once per mount
  as `{ name, hostId, sigilMarkup(label, size) }`, or `null` without a protected
  assistant session. `sigilMarkup` returns host-owned HTML. Without the capability
  it is `undefined`. It is not a callable action

The allowlist is an API convention, not a sandbox: trusted same-origin adapter
scripts can access page globals and existing bridges. SRI pins bytes; it does not
make untrusted code safe. See [Runtime catalog](../../docs/dashboards_view.md#runtime-catalog)
for retrieval rules, release steps and gate coverage.
