# Desktop configuration

Start with [Local setup](../README.md#local-setup), copy
`pentacle.config.example.js` to a private file, and select it with
`PENTACLE_CONFIG=/absolute/path/desktop.config.js`. A config is a JavaScript
module exporting an object. Restart the desktop after changing it.

The loader tries the explicit `PENTACLE_CONFIG` path alone when set. Otherwise
it tries `pentacle.config.js` beside the app, then the bundled example. An
explicit path typo is an error, not permission to silently use another config.
The loader only backfills missing `dark` and `terminal` objects from the example;
it does not merge the rest of an overlay with the example. The tables distinguish
example values from behavior when a key is omitted.

## Hosts and identity

With `chatStream.hosts: ['local', 'workstation']`, the titlebar and filters show
**Local / L** in forest green and **Workstation / W** in royal blue. No private
host inventory is built into the renderer. Configure actual daemon identities
and terminal transports when adding a host; a display label alone does not make
a remote machine reachable.

| Key | Type and default | Effect |
| --- | --- | --- |
| `hostNames` | Object of host ID → string; omitted | Explicit display labels; otherwise uppercase the first character of the resolved identity. Remaining case and hyphens are preserved. Explicit labels are trimmed but retain case. |
| `hostColors` | Object of host ID → colour token; omitted | Explicit colour; otherwise choose by roster index from `forest-green`, `royal-blue`, `red`, `orange`, wrapping after four hosts. |
| `localHostId` | String; `chatStream.hostMap.local`, then `chatStream.localHost`, then `local` | Local presentation/microphone identity. It does not change the terminal transport. |
| `chatStream.hosts` | String array; `['local']` | Ordered desktop host roster; order controls default colours. Use unique, nonempty IDs. |
| `chatStream.localHost` | String; `local` | Local terminal identity, also the fallback local presentation identity. |
| `chatStream.hostMap` | Object of desktop ID → daemon ID; `{}` | Exact routing aliases. Example: `{local: 'laptop', remote: 'workstation'}`. Overrides inference from the local identity. |

`local` is an alias: with roster `['local','workstation']` and localHost
`laptop`, its label/badge are Laptop/L. The resolved identity need not occupy a
second roster entry. For labels and colours, a mapping under the desktop ID
takes priority over one under its resolved identity. `localHostId` takes
priority for the local display/mic identity; `hostMap` takes priority for
routing. Reverse routing searches the configured roster for an exact mapped
identity. Names are never matched by substring. An empty identity falls back
to `local`; an unlisted identity uses the first palette colour. Badges use the
first Unicode code point of the display label, uppercased.

Supported colour tokens are `forest-green`, `royal-blue`, `red`, `orange`,
`green`, `blue`, `purple`, `yellow`, and `cyan`. Unsupported tokens use the
indexed default. Keeping roster order stable keeps default colours stable;
use explicit maps when colours should survive reordering.

Chat header ornaments follow the configured host colour independently of its
label: green selects the djinni, blue/cyan the mage, red/yellow the sun, and
orange/purple the flower. Renaming a host preserves its ornament.

## Connection and terminal transport

| Key | Type and default | Effect |
| --- | --- | --- |
| `chatStream` | Object; example values below | Connection, authentication, subscription and host settings. Omit `url` and public main does not connect. |
| `chatStream.url` | String; example `ws://127.0.0.1:7791` | Daemon WebSocket endpoint. |
| `chatStream.tokenPath` | String; example `~/.config/pentacle/desktop.token`; omitted legacy fallback `~/.config/pentacle-stream/token` | Credential file. For v2, use a regular mode-0600 file in a mode-0700 directory without symbolic-link ancestors; Windows uses the equivalent private-file ACL checks. |
| `chatStream.token` | String; absent | Legacy inline token, preferred over tokenPath if supplied. V2 credentials require a private tokenPath and cannot be inline. Do not commit either credential. |
| `chatStream.recentLimit` | Number; main defaults to 500, caps at 500; renderer defaults to 5000 when omitted | Retained recent event limits. Use a positive integer; configure 500 for consistent desktop buffers. |
| `chatStream.heartbeatMs` | Number; 30000 | Socket heartbeat interval, clamped to 5000–120000 ms. Nonfinite input uses 30000. |
| `chatStream.openedByLocal` | Boolean; false | When true and hostMap.local exists, subscribe only to sessions opened by that identity. |
| `chatStream.snapshot` | Boolean; true | Set false to omit the initial snapshot. Normal desktop setup should retain true. |
| `tmux` | String; `tmux` | Local executable and default remote tmux executable. |
| `hosts` | Object of host ID → transport object; `{}` | SSH terminal transports for nonlocal IDs. Each entry has `host` (required string), `user` (local OS username), `port` (22), `tmux` (top-level tmux). |
| `remote` | Transport object; absent | Legacy transport for the literal ID `remote`, also the client-mode marker. Fields match a hosts entry. |
| `localWsl` | Object; absent | Windows only: attach the local terminal inside a WSL distribution. `distro` (required) names the distribution, `user` the WSL account (omit for the distribution default), `tmux` the tmux executable inside it (top-level tmux). Every local tmux command runs as `wsl.exe -d <distro> [-u <user>] -- /bin/bash -lc '<tmux ...>'`. |

Public main attaches terminals locally when the selected ID is `local` or
equals chatStream.localHost. Other IDs need an entry in `hosts` (or `remote`
for that alias). Host labels and `localHostId` do not alter this decision.
On Windows, where the daemon and tmux usually live in WSL, set `localWsl.distro`
so those local attachments run inside that distribution; without it, main runs
the `tmux` executable on the Windows host directly.
The daemon owns session creation, provider executables, working directories,
provider models and effort. See [daemon setup](../services/chat-stream-v2/README.md).

## Features, microphone and metrics

| Key | Type and default | Effect |
| --- | --- | --- |
| `features` | Object; example flags below | Defaults before saved Settings overrides. |
| `features.chatUi` | Boolean; false | Experimental structured Chat. Terminals are the default session surface. Reload after changing. |
| `features.inputBar` | Boolean; example true | Retained compatibility flag; currently unused. |
| `features.usage` | Boolean; example false | Sidebar usage setting. Explicit false excludes live limits.update subscription frames; enabling it requires reload. Cached snapshot values may still paint. |
| `features.dashboards` | Boolean; false | Dashboard/widgets controls; actual backends require external adapters. |
| `features.mic` | Boolean; false | Microphone panel and voice controls; requires a running mic server. Reload after changing. |
| `features.sourceTags` | Boolean; false | Host tags on sessions. Reload after changing. |
| `features.showTurnDuration` | Boolean; false | Timing annotations in Chat; changes live. |
| `features.rawTmux` | Boolean; example false | Retained compatibility flag; currently unused. |
| `mic` | Object; absent | Optional microphone settings below. |
| `micServerUrl` | String; `http://127.0.0.1:7780` | Mic HTTP endpoint unless useStreamHost is enabled. |
| `mic.useStreamHost` | Boolean; false | Derive `http://<chatStream.url hostname>:7780`. Missing/invalid chatStream URL falls back to micServerUrl. |
| `mic.alwaysOnEnabled` | Boolean; false | Show the always-on controls. |
| `mic.autoSpawn` | Boolean; helper default true | Compatibility-only in public main: the desktop probes the service; it does not start a mic server. |
| `wakeWord` | String; absent | Text displayed in the sleeping mic state. Set it to the server's actual configured wake word. |
| `machineStats` | Object; absent, ignored | Accepted compatibility key for old overlays. No fields in this object configure public desktop stats. |

Microphone controls require an independently installed, running compatible HTTP
service. This checkout does not yet include a runnable microphone server
entrypoint; keep `features.mic: false` unless you already operate that endpoint.
Set its URL (or useStreamHost), verify its `/status` response, then enable mic. The request caller follows localHostId → hostMap.local →
chatStream.localHost → local; it never guesses from your OS hostname.

Machine-stat cards render the `hosts` payload in daemon `hosts.stats` frames and are
hidden when none exists. Usage values come from the daemon limits collector;
enabling a flag does not install that collector. With a valid retained limits
pair, collector errors keep the previous numeric values visible and show an
error message above the cards. A successful refresh clears the error. Missing
values remain dashes; a malformed limits/health pair cannot overwrite the last
valid pair. The desktop displays diagnostic text as plain text, capped at 500
characters. See [limits health contract](#limits-health-contract).

## Other desktop inputs

| Key | Type and default | Effect |
| --- | --- | --- |
| `appName` | String; example `Pentacle` | Window/titlebar/application name. Keep it present in a custom overlay. |
| `appId` | String; example `com.pentacle.app` | Compatibility metadata; runtime public main does not apply it. Packaging uses package.json build.appId. |
| `agents` | Object keyed by provider; example claude/codex labels | Provider menu labels and optional initial text. Each entry accepts `label` (provider label), `startupMessage` (absent), `startupDelayMs` (1500). A configured startupMessage is sent after a new session attaches. |
| `dark` | Theme object; bundled example | Retained theme input, backfilled as an object when missing. Public renderer appearance is controlled by Settings/CSS; this object does not override its palette. |
| `terminal` | Xterm theme object; bundled example | Terminal colours. Recognized fields: background, foreground, cursor, cursorAccent, selectionBackground; black/red/green/yellow/blue/magenta/cyan/white and their bright-prefixed counterparts. Exact bundled hex defaults are in the example. Missing objects are backfilled; partial objects are not deep-merged. |
| `artifactDirs` | String array; `[<working directory>/test/artifacts]` | Search roots for the retained local UI-review artifact helper. Leading `~` is expanded. |
| `repoRoots` | String array; `[]` | Additional repository parents for the artifact helper; scans each child repository's .ui-review and test/artifacts directories. |
| `dashboardHub` | Object; absent | Preload exposes it to optional dashboard adapters when `url` (string) is set. No bundled hub is started. Additional adapter-owned fields pass through. |

The retained `hosts.js` helper accepts the following legacy inputs. Current
public main uses `hosts`, `remote`, `tmux` and `localWsl` through terminal_adapter
instead; the other helper inputs do not configure that terminal path.

| Key | Type and helper default | Effect |
| --- | --- | --- |
| `localTmux` | String; macOS `/opt/homebrew/bin/tmux`, otherwise `tmux` | Helper's local tmux executable. |
| `localSsh` | Object; absent | Windows helper's local SSH: host required, port 2222, user from localWsl.user or null, tmux from localWsl.tmux or `tmux`. |
| `localWsl` | Object; `{distro:'Ubuntu', user:null, tmux:'tmux'}` | Windows helper's WSL transport defaults. Public main reads the same `distro`, `user` and `tmux` fields for local terminal attachment (see the transport table above); the helper default distro does not apply there. |
| `peers` | Object array; `[]` | Helper peer entries require id, host, user; port defaults 22 and tmux defaults `tmux`. |

Main adds these read-only IPC fields; they are not user-config keys:
`hostIds` (chatStream.hosts or `['local']`), `isClient` (remote present),
`hostname` (OS hostname), `platform` (OS platform), `configError` (error message
or null), and `configWarnings` (warning objects). Main removes chatStream token
and tokenPath before returning its configuration over IPC.

## Configuration warnings

Warnings are advisory and do not prevent launch. The loader returns
`[{code, message}]`; main logs each code once per config path to stderr with a
`desktop-config` prefix and returns the same warnings in get-config. A dedicated
banner shows their plain-text messages until reload; an empty list hides it.
It is separate from the daemon connection-status banner.

| Code | Exact condition | Resolution |
| --- | --- | --- |
| `multi-host-presentation` | hosts has more than one entry and hostNames or hostColors is absent, null, an array, or a nonobject | Add either missing map. Empty/partial objects are allowed; absent per-host entries use defaults. |
| `mic-endpoint` | features.mic is true, micServerUrl is blank/absent, and useStreamHost is not true with a nonblank chatStream URL | Configure the endpoint. Merely setting `mic: {}` does not suppress this warning. |
| `unknown-top-level` | One or more top-level keys are outside the active/compatibility inventory above | Correct typos or remove obsolete keys. Only key names are logged, never values. |

This is a targeted diagnostic, not full schema validation. It does not check
reachability, credentials, every nested field, or completeness of host maps.

## Upgrading an existing install

Keep your private overlay outside the app/repository and keep selecting it with
PENTACLE_CONFIG after replacing the package. Preserve hostNames, hostColors,
localHostId, hostMap, mic/micServerUrl and your feature flags when those explicit
choices differ from the defaults. A legacy machineStats object may remain but
does not configure cards. Use config mappings for old local/remote aliases.

Settings overrides are stored under `pentacle.settings.v1` in the renderer's
localStorage. They override config defaults both before and after asynchronous
configuration arrives. Existing saved Chat opt-in therefore survives an upgrade
and a config containing chatUi:false. Change the toggle in Settings and reload
to opt out. Preserve the application profile to retain these overrides; a new
profile starts with config defaults.

## Limits health contract

Desktop snapshots and limits.update frames carry a complete three-row limits
array ordered Claude (`claude`), Fable (`fable`), Codex (`codex`). Each row has
id, label, pct, resets_at_iso, resets_text, upstream_reported_at, and probed_at.
Percentages are null or integers 0–100. Timestamp/order/schema validation is
shared between the socket cache and renderer; a valid pair updates atomically.
Without a prior valid pair the cache starts with three null rows and null health.

Health is null, absent for older daemons, or
`{schema_version:1, claude:{attempted_at, outcome, error, upstream_reported_at,
probed_at, stale_after_seconds}}`. The threshold is an integer 60–86400 seconds;
timestamps are UTC RFC3339 or null as permitted by the outcome. `never` has null
timestamps/error; `ok` has observation timestamps and no error. Failure keeps
the last observation timestamps and supplies an attempted_at and error.

For provider_error, the accepted codes are `usage_provider_error`,
`claude_usage_provider_error`, `codex_usage_provider_error` (Codex row), and
legacy `claude_subscription_unavailable`; message must be a nonblank string.
Other supported failures retain fixed pairs:

| Outcome | Code | Message |
| --- | --- | --- |
| auth_error | claude_not_authenticated | Claude is not authenticated |
| parser_error | claude_usage_parse_failed | Claude usage could not be parsed |
| timeout | claude_usage_timeout | Claude usage probe timed out |
| transport_error | claude_usage_transport_failed | Claude usage transport failed |
| internal_error | claude_usage_internal_error | Claude usage probe failed internally |
| store_error | usage_state_write_failed | Usage state could not be saved |

A publisher must include limits_health in live updates and publish health-only
changes even when retained numeric values are unchanged. Invalid state files
must retain the last published pair. This allows startup, live failure,
handshake buffering and healthy refresh to follow the same contract.

The collector fills these rows by running one shared probe per provider under
`scripts/`: `check_claude_usage.py` drives the Claude CLI `/usage` screen for
the weekly Claude/Fable rows, and `check_codex_usage.py` drives `codex
app-server` (stdio JSON-RPC `account/rateLimits/read`) for the weekly Codex
row, selecting the window whose `windowDurationMins` is at least a week. The
Claude probe binary comes from `PENTACLE_USAGE_CLAUDE_BIN` (the deploy sets it to
the Claude shim); the Codex binary is found on `PATH` (the collector's launchd
PATH includes the Codex install dir), so Codex adds no dedicated knob. Codex
`resets_at_iso` is authoritative UTC while `resets_text` renders in the host's
local timezone. A probe that cannot reach its CLI exits non-zero, recording
`provider_error` for that row while the other provider's fresh value and this
row's prior value are retained. A silent/hung probe is bounded by the
collector's 75s per-probe subprocess timeout (same `provider_error` result).
