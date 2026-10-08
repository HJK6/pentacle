# Optional dashboards

Dashboard adapters register only when their existing configuration conditions permit it. Each adapter receives caller-supplied state and actions. Keep endpoints, credentials and captured records in local configuration, outside Git.

The bounded library in [`vendor/dashboard-library`](../../vendor/dashboard-library/README.md) provides six consumed boards: specs, foreclosure, chat-stream, ui-review, notifications and 0dte-trading. Business, mobile-testing, pi-control and scraper-bot remain self-contained. Loading the library exposes `PublicDashboardLibrary` and the legacy `TriforceDashboards` alias used by existing adapters; it does not register adapters in `window.DASHBOARDS`.

Definitions declare an ID and manifest plus either `render(state, ctx)` or `mount(container, ctx)`, `update(refs, state, ctx)` and `unmount(refs)`. The context carries `mode`, visibility checks and explicitly supplied action callbacks. Display mode hides interactive-only controls. Escaping and cleanup belong to the definition; a callback receipt and refreshed producer state determine write success.

Generate and verify the dependency with its `npm run build`, `npm run check:dist` and `npm test`. Do not edit generated files independently or silently load private producer modules. Missing shared definitions or writers must remain visibly unavailable.

Personal boards arrive at runtime through the operator's `dashboard-catalog` asset and the web host's authenticated `/dashboards/private/<catalog_version>/` route; see [Runtime catalog](../../docs/dashboards_view.md#runtime-catalog). Public source ships no personal board ids, endpoints or bundles.

## Visibility and selection

Adapters self-register in `window.DASHBOARDS`. Their optional `retired: true`
manifest flag keeps the implementation registered but hides it from the list by
default. Foreclosure Pipeline and Scraper Bot are retired; their original
configuration conditions still determine whether they register at all.

`window.visibleDashboards(config)` applies `dashboards.hidden` (an array of exact
IDs) and `dashboards.showRetired` (only boolean `true` enables retired boards).
Explicit hidden IDs win. Active boards come first, then retired boards, retaining
registration order within each group. A previously selected hidden board is
replaced by the first visible board. When only opted-in retired boards remain,
the first one is selected; no visible boards produces “No dashboards configured”.
Visibility settings take effect after the app/web host is restarted and reloaded.

The built-in `modeler-3d` adapter is always registered, including when unconfigured.
The view passes the resolved renderer config in the mount context described below.
It owns its navigation status and does not poll; `unmount` removes its iframe,
event handlers and timeout. Existing adapters may ignore the optional context.
See [Dashboards view](../../docs/dashboards_view.md) for viewer configuration,
state meanings, security limits and the synthetic browser gate.

## Runtime catalog client

Set `dashboards.catalogSpecId` to a synthetic example such as
`example__dashboard_catalog` in an operator-owned, gitignored local profile.
Keep the actual spec id, endpoints, tokens and catalog root out of public source.
Restart/reload after changing that local setting. The catalog's content is
fetched on Dashboards view entry, after app boot; publishing a newer catalog
needs no renderer rebuild or host restart. There is no catalog polling loop.

`catalog_loader.js` discovers only the exact `dashboard-catalog` asset with the
matching content type, gets it through its listed owner `stream_id`, and ports
the daemon's schema validation before merging boards after the built-ins in
array order. A duplicate built-in id produces a board error. With the setting
unset there is no catalog request; the existing `shared-demo` and `modeler-3d`
built-ins remain. If visibility settings hide all boards, the existing “No
dashboards configured” state appears.

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
- `hosted-view` reuses the existing modeler iframe lifecycle, sandbox, timeout,
  reload and open-in-new-window controls with the catalog's id/name/URL. It does
  not change how the built-in modeler is configured

The stable view hook is `#dashboard-content[data-catalog-version]`; board state
is `data-board-state` (`loading`, `ready`, `empty`, `partial`, `error`, or
`unsupported`). List buttons use `data-dashboard-id`. Error/report elements use
`data-testid="dashboard-catalog-error"`, `dashboard-catalog-unavailable`,
`dashboard-board-unsupported`, `dashboard-board-error`, `dashboard-report-latest`,
`dashboard-report-list`, `dashboard-report-truncated`, and `dashboard-report-empty`
as applicable. Web supports all three catalog kinds; the unsupported-board hook
is for clients that cannot render a kind.

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
