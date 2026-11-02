'use strict';
// External daemon only; the operator token remains in its existing secure file.
const url = process.env.PENTACLE_CARD_PROBE_WS_URL;
const tokenPath = process.env.PENTACLE_CARD_PROBE_TOKEN_PATH;
if (!url || !tokenPath) throw new Error('PENTACLE_CARD_PROBE_WS_URL and PENTACLE_CARD_PROBE_TOKEN_PATH are required');
module.exports = {
  appName: 'Pentacle', features: { mic: false }, tmux: 'tmux',
  hosts: { thoth: { kind: 'local' } }, agents: {},
  chatStream: { url, localHost: 'thoth', hosts: ['thoth'], tokenPath },
};
