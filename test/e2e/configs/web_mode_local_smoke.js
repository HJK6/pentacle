'use strict';

// Gate config for the web-mode smoke. Points the desktop and the headless web
// host at a loopback chat-stream daemon, so the gate never touches shared
// infrastructure. Start one by pasting this verbatim:
//
//   SCRATCH=$(mktemp -d)
//   python3 services/chat-stream-v2/main.py --host 127.0.0.1 --port 7796 \
//     --local-host local --db "$SCRATCH/sessions.db" \
//     --notifications-db "$SCRATCH/notifications.db" \
//     --assets-db "$SCRATCH/assets.db" --blob-root "$SCRATCH/blobs" \
//     --disable-hosts --disable-mirror --disable-nudges \
//     --disable-outbound-notices --disable-remote-presence
//
// `--local-host local` matches the `chatStream.localHost` below; the port must
// match the url below. If 7796 is busy, change both.
//
// Run with:
//   node test/e2e/web_gate.js --profile test/e2e/configs/web_mode_local_smoke.js
//
// Point --profile at any other config file to run the same smoke against a
// different daemon; nothing here is machine-specific.

const path = require('node:path');

module.exports = {
  appName: 'Pentacle',
  features: { mic: false },
  tmux: 'tmux',
  hosts: { local: { kind: 'local' } },
  agents: {},
  chatStream: {
    url: 'ws://127.0.0.1:7796',
    localHost: 'local',
    hosts: ['local'],
    // The loopback daemon trusts 127.0.0.1 and keeps no credential registry, so
    // point the client at a path with no operator envelope.
    tokenPath: path.join(__dirname, 'no-such-operator-token'),
  },
};
