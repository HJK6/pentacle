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

## Validation

Run `npm test`, `npm run build:web`, and `node test/e2e/web_gate.js` from the
repository root. `npm run prestart` builds renderer prerequisite bundles when
needed; the web build also ensures those prerequisites exist.

New coverage is collected from `test/dashboards_visibility.test.js`,
`test/modeler_3d_dashboard.test.js`, and `test/dashboards_scenario.test.js`.
The existing four dashboard suites remain unchanged. Adapter tests use synthetic
frame events and deterministic timers; they do not establish browser behavior.

The 16th web scenario, `web-dashboards-revamp`, registers the real retired
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
success do not certify the 16 browser scenarios. The fleet runs that gate and CI
after applying the format-patch series when local browser execution is blocked.
