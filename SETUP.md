# Pentacle setup

Follow [Quick start in README](README.md#quick-start) to install dependencies,
start the daemon and serve the web client with `npm run build:web`. See
[the web host reference](server/README.md).

Copy `pentacle.config.example.js` to a private file and select it with
`PENTACLE_CONFIG`. Use the [web configuration reference](docs/desktop_config.md)
for host names, colours, local identity, terminal transports, microphone and
limits settings. It also explains config warnings and upgrading an existing install.

For setup performed by an agent, follow [the agent checklist](AGENT_SETUP.md).
For remote clients, read [network and authentication setup](docs/REMOTE_AUTH.md).

Finish with `bin/pentacle doctor --live`. Use [UPGRADE.md](UPGRADE.md) to upgrade
an existing install and `bin/pentacle version` to check installed identities.
