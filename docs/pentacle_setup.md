# Pentacle web client topology

The supported desktop experience is Pentacle's web client in a browser or
installed as a PWA. The Electron app is deprecated; it receives no further
upgrades, packaging or rollout. The web host serves the renderer and connects to
the separately configured chat daemon, which owns agent processes and session
working directories.

## Local setup

Follow [developer onboarding](developer_onboarding.md) for a scratch daemon and
web host. The root [local setup](../README.md#local-setup) is the complete
developer walkthrough. The web host prints the local URL to open in a browser;
for a shared HTTPS deployment, use the [web host guide](../server/README.md)
and its authentication requirements.

## Host profiles and terminals

Choose a browser profile with `node server --profile <name>`. Keep private
endpoints, host inventories and credentials in an untracked
`configs/<name>.local.js`; keep tracked examples generic. The [profile guide](../configs/README.md)
covers profile selection and shape, while [configuration reference](desktop_config.md)
covers shared client settings.

The web host talks to the daemon over its configured WebSocket URL. The daemon
creates sessions and runs providers; the web profile maps daemon host IDs to
terminal transports. See
[web host profiles](../configs/README.md#browser-profile-shape)
and [daemon setup](../services/chat-stream-v2/README.md) before adding remote
machines. A display label alone does not configure a terminal transport.

## Web client controls

Machine sigils identify the host for each session. Each terminal slot has a
Copy Chat ID control in its header. Provider usage appears in the sidebar when
`features.usage` is enabled and the daemon limits collector supplies data. Once
the host serves a build with update checking, each open browser window
independently shows a refresh control beside Settings when that window's loaded
build is older; refreshing updates that window.

The top and bottom terminal rows have independent column dividers, and each row
keeps its preferred split on this browser origin; the [shared configuration
reference](desktop_config.md) covers divider controls and persistence. In the
experimental structured Chat view, durable question cards support typed
free-text answers, including prompts without choices.

The web client also supports microphone input and voice actions through a
configured host-managed service. See [local voice action delivery](local_voice_actions.md)
for the current behavior and setup.

## Verification

| Symptom | First check |
|---|---|
| Sidebar is empty | Check the WebSocket URL and `agent-orch list`. |
| Experimental structured Chat is unavailable | Enable `features.chatUi` and inspect the daemon health result. |
| Terminal attach fails | Check the selected host ID and its configured tmux transport. |
| Local daemon exits | Run the daemon command directly and inspect its startup error. |
