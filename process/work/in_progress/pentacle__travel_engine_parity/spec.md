---
id: spec_pentacle__travel_engine_parity
title: Travel engine parity in Pentacle
type: spec
status: in_progress
canonical: false
created_at: '2026-09-28'
updated_at: '2026-09-28'
source_path: work/in_progress/pentacle__travel_engine_parity/spec.md
machine: local
owner: codex
tags:
- pentacle
summary: Expose the full standalone travel engine inside Pentacle, including complete
  airport autocomplete, without maintaining a second implementation.
related:
- work_pentacle__travel_engine_parity
---

# Travel engine parity in Pentacle

## Status

In progress, 2026-09-28. Independent spec QA accepted candidate `6f1d3952186e8070b93022a2564443154e2c66fd02880bce0b1a1088192e93f9`; scope and validation plan are locked. The work item retains the full parity goal and keeps live provider and purchase activity out of scope.

## Ownership

- Authored on: local
- Implementation: local (Pentacle desktop/web app plus the sibling `travel-engine` frontend source)
- Driver: codex

## Tracking

- Lookup title: Travel engine parity in Pentacle
- Status: in_progress
- Completion rule: satisfy the acceptance criteria and disposition the changes under `process/docs/config/development_process.md`.

## Goal

Make the existing full Travel Engine interface available from Pentacle so users get the same search, wallet, vault, itinerary, filtering, share, and booking flows as the standalone app. The feature comes from the original page and service, not a recreated subset. Fix airport-code autocomplete so every airport whose code matches the typed prefix is reachable from the scrollable dropdown, including matches beyond the current 12-item display cap.

## Current State

- The Pentacle renderer has a generic Dashboards view and a Shared Demo board. It has no Travel Engine board or travel service URL setting. The default `features.dashboards` value is false and remains the existing opt-in gate.
- The standalone engine lives in the sibling `../travel-engine` repository. FastAPI serves `web/index.html`, `web/airports.json`, and the search, wallet, vault, share, and booking APIs from one origin. Its frontend has Search, Wallet, and Vault views and renders points and cash itineraries.
- `web/index.html` currently truncates the combined airport/metro suggestions to 12. Direct execution of its actual `acMatches` function with the checked-in datasets confirmed that `a` has 352 airport-code prefix matches but returns 12 metro-name suggestions and zero airport codes; `ab` has 21 airport-code matches but returns only 12. The exact source and dataset hashes and reproduction are in `_artifacts/autocomplete_baseline.txt`.
- The engine is not running at the local default URL `http://127.0.0.1:8777` during diagnosis. Pentacle has no active app window in this session. The live journey is not observed. Starting `app.main:app` would auto-unlock the remembered vault key and launch the persistent Playwright browser manager, so validation will use isolated/offline tests and a static HTTP fixture instead of touching the user's provider profiles.
- `services/dashboard-hub/` and `services/lews/` are untracked service trees in the root checkout. They are outside this change and will remain untouched.

## Scope Reassessment

The broad parity goal remains the right scope: the standalone page and service are the source of truth, so the Pentacle dashboard should embed that page rather than recreate a subset. Narrowing acceptance to the existing UI suite would leave Wallet, Vault, and booking review unverified, so the revised plan adds a two-origin browser fixture with in-memory responses for the APIs those views call. Existing offline backend tests continue to cover service behavior. The fixture will use only synthetic data and will assert the final purchase endpoint is never called.

The same-origin sandbox gap is addressed by rejecting a configured engine origin equal to the Pentacle page origin, in addition to the existing sandbox restrictions. Service availability is defined by a validated page-ready message with a bounded timeout, not the iframe `load` event. Autocomplete coverage is made data-derived and exhaustive for every distinct 1–3 character airport/metro code prefix. The existing UI test target will write its snapshot and database into a temporary directory and remove them on exit.

## Target State

- Pentacle registers a Travel Engine dashboard backed by a private `travelEngine.url` config value. The dashboard embeds the standalone engine's root page on its own origin, so its data assets and API calls continue to work without a second frontend or a cross-origin API proxy.
- The dashboard reports a clear setup or unavailable state when its URL is missing or the service cannot be reached. It does not start or install the external service. Its URL contract is an absolute HTTP(S) origin with only an optional `/` path; reject credentials, non-root paths, query strings, and fragments. When Pentacle has a non-opaque browser origin, reject a configured engine URL with the same origin. Allow HTTP in the Electron `file://` app; when Pentacle itself is served over HTTPS, require an HTTPS engine URL to avoid mixed content.
- The integration is present in both Electron and the generated browser build. The user can enable the existing Dashboards feature and open Travel Engine from the board list.
- The iframe has exactly the sandbox grants `allow-scripts allow-same-origin allow-forms allow-modals`, no popup or top-navigation grant, and `allow="clipboard-write"` for the share flow on supported secure origins. In standalone use, external booking links retain their current behavior. In embedded use, the child sends a `travel-engine:open-external` message for a clicked HTTP(S) booking link. Pentacle accepts it only when the message source is the mounted iframe, the origin exactly matches the configured engine origin, the payload has the documented type and URL string, and the destination is absolute HTTP(S). Other sources, malformed payloads, and non-HTTP(S) URLs are ignored.
- After loading its program and airport data, the embedded engine sends `travel-engine:ready` to its parent. Pentacle accepts that message only from the mounted iframe and the configured origin. If no valid ready message arrives within 10 seconds after the frame is assigned its source, the dashboard shows an actionable unavailable state with a Retry control. A late valid ready message recovers the dashboard. The timeout—not `iframe.onload`—is the failure signal.
- For every distinct one-, two-, or three-letter prefix derived from airport and metro codes, the dropdown includes exactly all airport and metro codes that start with that prefix; code matches appear before text matches and cannot be displaced by them. Name searches remain capped at 12 suggestions. The list stays scrollable; Arrow navigation scrolls the highlighted option into view, and Enter or a click selects a result near the end of the full code-match set.

## Plan

1. Complete independent spec QA and lock the acceptance and validation plan.
2. Add and validate the optional service URL config; register the dashboard in desktop and browser builds with page-ready and external-link message handling.
3. Update the standalone airport matcher and add exhaustive prefix and full-list keyboard regression checks against the real airport/metro datasets.
4. Make `make test-ui` isolate snapshot/database outputs in a temporary directory and clean them on success or failure.
5. Add a two-origin HTTP fixture with synthetic in-memory search, balance, vault, share, and safe booking-review responses; test the actual Pentacle dashboard adapter and standalone page in an isolated Playwright context.
6. Document setup and feature boundaries; build the browser bundle; run the Pentacle checks and the standalone engine's offline backend/UI checks; record exact artifact, harness, configuration, and environment identity.

## Validation

- Pentacle: add focused tests for accepted/rejected URL forms, credentials/path/query/fragment, same-origin rejection, mixed-content behavior, configured/unconfigured/unavailable/late-ready states, the 10-second timeout and retry, exact iframe sandbox grants, message source/origin/shape checks, destination scheme checks, clipboard permission, and browser-build script/config inclusion. Run the focused test and `node --test test/web_bundle.test.js`; run `npm run build:web` and inspect both generated HTML and copied dashboard script.
- Autocomplete: add a test against the checked-in `web/airports.json` and `web/metros.json` that derives every distinct one-, two-, and three-character code prefix from both datasets. For each prefix, assert the result-code set equals the expected prefix set and precedes any text matches; assert name-only search returns at most 12. In a real browser fixture, type a one-letter prefix with more than 12 matches, Arrow through the last code-prefix result, verify it scrolls into view, and select it with Enter. Also check a metro-code result and a candidate by click.
- Standalone suites: run `make test` and `make test-ui`. Change the `test-ui` target to put `ui_snapshot.json` and `ui_snapshot.db` under a newly created temporary directory with a cleanup trap, so existing `data/` files are neither overwritten nor removed. Verify the temporary outputs are gone after the command.
- Embedded browser fixture: run two loopback HTTP origins (Pentacle parent and Travel Engine child) so the test exercises the cross-origin sandbox. Serve the actual engine `web/` page and checked-in assets. Stub these same-origin engine APIs with synthetic in-memory responses: search creation/poll/share, balances and manual balance changes, vault status/init/unlock/lock/cards/secrets, and booking create/poll/screenshot. Build search results from the existing checked-in source fixtures. Use an isolated, non-persistent Playwright browser context; do not invoke the real FastAPI app or provider browser manager. Verify airport results, all six source result categories, Points/Dollars, filters/layouts/itinerary detail, share response, Wallet read/update, Vault create/unlock and card/login views, booking review screen, and external deep-link dispatch through the real dashboard adapter with `openExternal` mocked. Assert no `/api/book/{id}/confirm` request occurred. Abort the child request to verify the unavailable timeout/Retry state, and verify the parent rejects a same-origin service URL. Stop only fixture processes created by the test and preserve raw receipts under `_artifacts/`.
- Run the Travel Engine's existing offline backend suite (`make test`) for API/service behavior; run the isolated UI suite (`make test-ui`) for its current result/grid/filter/drawer/share coverage. These local fixtures do not establish that live providers are available. Starting `app.main:app` is prohibited for this task because it auto-unlocks the remembered vault key and starts persistent provider browser profiles. Real provider logins, live searches, and a final purchase remain explicitly untested.

## Acceptance Criteria

### Common

#### Analysis
- [x] **Spec QA** — independent reviewer accepted candidate `6f1d3952186e8070b93022a2564443154e2c66fd02880bce0b1a1088192e93f9`.
- [x] **Validation plan locked** — acceptance criteria and validation plan approved before implementation.

#### In Progress
- [x] **Integration** — Travel Engine opens inside Pentacle from the Dashboards board list when configured, uses the original engine page and same-origin backend, and gives an actionable setup/unavailable state otherwise. Configuration, registry, setup/error states and browser build are covered by focused tests and the cross-origin fixture.
- [x] **Parity** — the embedded original page loads from its own service origin, including its existing API routes and client code for the six search sources (Frontier cash/Discount Den/GoWild/miles, Skiplagged, Momondo, Google Flights, Southwest cash/points, PointsYeah awards), Points/Dollars results, filters/layouts/itinerary detail, Wallet, Vault, sharing, and current assisted/deep-link booking flows. The offline backend and UI suites pass. Booking review controls are verified without invoking the final purchase action; live provider availability and purchase are not acceptance claims.
- [x] **Autocomplete** — tests derive all 6,770 distinct one-, two-, and three-character prefixes from the airport and metro datasets and assert exact code-prefix set equality; code matches precede and are not displaced by name results; name search stays capped; keyboard selection and click near the end of the complete list work and auto-scroll.
- [x] **Embed contract** — tests cover configured/unconfigured/unavailable/late-ready states, the exact 10-second timeout constant with accelerated timeout behavior and retry, absolute HTTP(S) root URLs without credentials/query/fragment, same-origin rejection in browser pages, HTTPS-only engine URLs from HTTPS Pentacle pages, exact sandbox grants, clipboard permission, and rejection of message events with the wrong source, origin, shape, or destination scheme.
- [x] **Embedded UI coverage** — an isolated two-origin fixture covers search results from all six source categories, share, Wallet read/update, Vault create/unlock and saved-item views, safe booking review controls, airport autocomplete, and external deep-link dispatch through the adapter. Synthetic endpoints remain in memory, and the fixture proves no booking-confirm endpoint is called.
- [x] **Test isolation** — `make test-ui` writes its generated snapshot and database into a disposable temporary directory and removes them after success or forced failure; the forced-failure run left zero entries in its owned temp root. Pre-existing `data/` files were not modified.
- [x] **Build surfaces** — the desktop page loads the board adapter and the generated browser page contains the copied adapter; both load the configured URL contract.
- [x] **Code QA** — independent implementation review returned ACCEPT after checking the pinned code, tests, documentation, work record and preserved receipts.
- [x] **Validation passed** — all named tests/build checks pass; the missing `pytest-asyncio` test plugin was installed only in the ignored sibling virtualenv before the 318-test backend rerun. Runtime limitations and their prerequisites are recorded accurately.
- [x] **Documentation** — config/setup instructions describe the URL, external service prerequisite, dashboard feature flag, supported embedded behavior, and tested vs untested surfaces.

### Closure
- [ ] **Code disposition** — follow the development guide's shipped, tracked, or handed-off rule; no runtime activation is claimed unless bound to the actual process.
- [ ] **Working tree** — owned changes are identified; preserve the pre-existing untracked dashboard-hub tree.
- [ ] **Retro** — record lessons and `completed_at` in both work files at closure.

## Risks

- A configured external service can be unreachable or can change independently — show availability clearly and preserve the standalone app as the source of truth. Static fixture checks prove embedding and asset behavior, not provider availability.
- The iframe crosses the Pentacle origin boundary — reject same-origin service configuration, allow only the sandbox capabilities required by the existing app, and validate frame origin, source, message shape, and destination scheme before opening links.
- The broadest first-letter code queries produce hundreds of options — keep the dropdown scrollable and keyboard navigable, with deterministic major-airport ranking and auto-scroll to the highlighted item.
- The remote search adapters, credentials, and actual purchase flow depend on the separately operated travel service — do not claim those runtime paths passed without exercising them.

## Non-Goals

- Vendor or rewrite the travel backend, install or start it automatically, or alter its search providers, credential vault, booking decisions, or provider APIs.
- Modify the untracked dashboard-hub service or ship its private endpoint configuration.
- Change booking economics, submit a purchase, or log into any upstream provider.
