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
The view passes the resolved renderer config to `mount(container, { config })`.
It owns its navigation status and does not poll; `unmount` removes its iframe,
event handlers and timeout. Existing adapters may ignore the optional context.
See [Dashboards view](../../docs/dashboards_view.md) for viewer configuration,
state meanings, security limits and the synthetic browser gate.
