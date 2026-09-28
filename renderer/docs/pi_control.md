# Display control interface

The optional `dashboards/pi-control.js` adapter controls what a configured display shows. Choosing a display profile does not control a pipeline or change the desktop's selected dashboard.

Runtime configuration comes from `window.HOST.dashboardHubConfig` or `dashboardHub` in the config loader. It supplies `url`, `readTokenPath` and `writeTokenPath`; token files stay local and are read at runtime. The adapter uses supplied fetch/EventSource capabilities, displays connection state, and masks token query values in diagnostic URLs. A missing configuration cannot establish a live producer connection.

Profile changes use the rendered confirmation flow and configured writer. Profile IDs include `foreclosure-live`, `agent-output` and `specs`; `bart.control` is the assistant's control-envelope namespace, not a machine endpoint. Spec selection on the wall display remains separate from desktop navigation. Public source publication does not activate a display or provision credentials.
