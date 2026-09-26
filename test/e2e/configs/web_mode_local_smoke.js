'use strict';

// Gate config for the web-mode smoke. Points the desktop and the headless web
// host at a loopback chat-stream daemon, so the gate never touches shared
// infrastructure. Start one by pasting this verbatim:
//
//   export PENTACLE_SMOKE_PORT=49001  # pick a free, distinct port per run
//   SCRATCH=$(mktemp -d)
//   python3 services/chat-stream-v2/main.py --host 127.0.0.1 --port "$PENTACLE_SMOKE_PORT" \
//     --local-host local --db "$SCRATCH/sessions.db" \
//     --notifications-db "$SCRATCH/notifications.db" \
//     --assets-db "$SCRATCH/assets.db" --blob-root "$SCRATCH/blobs" \
//     --disable-hosts --disable-mirror --disable-nudges \
//     --disable-outbound-notices --disable-remote-presence
//
// `--local-host local` matches the `chatStream.localHost` below. The profile
// reads the same explicit port as the daemon. Stop that daemon and remove its
// SCRATCH directory when the smoke run ends.
//
// Run with:
//   node test/e2e/web_gate.js --profile test/e2e/configs/web_mode_local_smoke.js
//
// Point --profile at any other config file to run the same smoke against a
// different daemon; nothing here is machine-specific.

const path = require('node:path');
const rawPort = process.env.PENTACLE_SMOKE_PORT;
if (!rawPort || !/^[0-9]+$/.test(rawPort)) {
  throw new Error('PENTACLE_SMOKE_PORT must be a decimal integer in 1..65535');
}
const smokePort = Number(rawPort);
if (!Number.isSafeInteger(smokePort) || smokePort < 1 || smokePort > 65535) {
  throw new Error('PENTACLE_SMOKE_PORT must be a decimal integer in 1..65535');
}

module.exports = {
  appName: 'Pentacle',
  features: { mic: false },
  tmux: 'tmux',
  hosts: { local: { kind: 'local' } },
  agents: {},
  chatStream: {
    url: `ws://127.0.0.1:${smokePort}`,
    localHost: 'local',
    hosts: ['local'],
    // The loopback daemon trusts 127.0.0.1 and keeps no credential registry, so
    // point the client at a path with no operator envelope.
    tokenPath: path.join(__dirname, 'no-such-operator-token'),
  },
};
