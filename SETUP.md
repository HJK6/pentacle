# Pentacle setup

Follow [Local setup in README](README.md#local-setup) to install dependencies,
start the daemon and serve the web client with `npm run build:web`. See
[the web host reference](server/README.md). The Electron client remains
maintained; desktop rollout happens on demand.

Copy `pentacle.config.example.js` to a private file and select it with
`PENTACLE_CONFIG`. Use the [desktop config reference](docs/desktop_config.md)
for host names, colours, local identity, terminal transports, microphone and
limits settings. It also explains config warnings and upgrading an existing install.

For setup performed by an agent, follow [the agent checklist](AGENT_SETUP.md).
For remote clients, read [network and authentication setup](docs/REMOTE_AUTH.md).
