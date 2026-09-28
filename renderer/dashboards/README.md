# Optional dashboards

Dashboard adapters register only when their existing configuration conditions permit it. Each adapter receives caller-supplied state and actions. Keep endpoints, credentials and captured records in local configuration, outside Git.

The bounded library in [`vendor/dashboard-library`](../../vendor/dashboard-library/README.md) provides six consumed boards: specs, foreclosure, chat-stream, ui-review, notifications and 0dte-trading. Business, mobile-testing, pi-control and scraper-bot remain self-contained. Loading the library exposes `PublicDashboardLibrary` and the legacy `TriforceDashboards` alias used by existing adapters; it does not register adapters in `window.DASHBOARDS`.

Definitions declare an ID and manifest plus either `render(state, ctx)` or `mount(container, ctx)`, `update(refs, state, ctx)` and `unmount(refs)`. The context carries `mode`, visibility checks and explicitly supplied action callbacks. Display mode hides interactive-only controls. Escaping and cleanup belong to the definition; a callback receipt and refreshed producer state determine write success.

Generate and verify the dependency with its `npm run build`, `npm run check:dist` and `npm test`. Do not edit generated files independently or silently load private producer modules. Missing shared definitions or writers must remain visibly unavailable.
