'use strict';
// External daemon only; the operator token remains in its existing secure file.
const url = process.env.PENTACLE_CARD_PROBE_WS_URL;
const tokenPath = process.env.PENTACLE_CARD_PROBE_TOKEN_PATH;
const host = process.env.PENTACLE_CARD_PROBE_HOST;
if (!url || !tokenPath || !host) throw new Error('PENTACLE_CARD_PROBE_WS_URL, PENTACLE_CARD_PROBE_TOKEN_PATH and PENTACLE_CARD_PROBE_HOST are required');
module.exports = {
  appName: 'Pentacle', features: { mic: false }, tmux: 'tmux',
  hosts: { local: { kind: 'local' } }, agents: {},
  chatStream: { url, localHost: host, hosts: ['local'], tokenPath },
};
