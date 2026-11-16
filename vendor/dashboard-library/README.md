# Optional dashboard library

Bounded projection from `triforce-dashboards` commit `26a9c00c089e42c8cabbef297152dc75cb83aabe` (UNLICENSED). The exact upstream package was private and unavailable from the public npm registry at review. It has no package dependencies.

Included: shared factory/helpers/CSS and specs, foreclosure, chat-stream, ui-review, notifications and 0dte-trading. Demo, OCR and email-health are excluded. Original inclusive source segments: [(1, 223), (260, 3044), (3334, 4208), (4443, 4467)]. Synthetic witnesses are adapted to this boundary.

Run `npm run build`, `npm run check:dist` and `npm test` here. VERSION is the first 12 hexadecimal characters of SHA-256 over raw JavaScript, a NUL separator and CSS. Commit source and matching generated artifacts together.

Consumers explicitly load the generated classic script, exposing `PublicDashboardLibrary` and the existing adapter-compatible `TriforceDashboards` alias; this directory does not register dashboards or activate a producer. Data, credentials and action callbacks come from the consumer. Local configuration and captured evidence stay out of Git.
