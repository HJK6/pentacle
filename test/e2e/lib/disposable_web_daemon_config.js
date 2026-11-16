'use strict';

// Small public fixture helper for loopback browser gates. It intentionally
// has no dependency on the private scripted-daemon launcher or fleet paths.
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '../../..');

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function writeDesktopConfig({ dir, wsUrl, tokenPath }) {
  if (!dir || !wsUrl || !tokenPath) throw new Error('disposable daemon config requires dir, wsUrl and tokenPath');
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const configFile = path.join(dir, 'pentacle.disposable-web.config.js');
  const baseConfig = path.join(ROOT, 'pentacle.config.example.js');
  const body = `'use strict';
const base = require(${JSON.stringify(baseConfig)});
module.exports = {
  ...base,
  features: { ...base.features, chatUi: true, chatHarnessTelemetry: true,
    usage: false, mic: false, dashboards: false, sourceTags: true },
  chatStream: { ...(base.chatStream || {}), url: ${JSON.stringify(wsUrl)},
    tokenPath: ${JSON.stringify(fs.realpathSync(tokenPath))}, autoStart: false,
    hosts: ['mock-host'], machinesFile: '', recentLimit: 5000,
    hostMap: { local: 'mock-host' } },
  hostNames: { 'mock-host': 'Mock Host' },
  hostColors: { 'mock-host': 'green' },
  machineStats: { ...(base.machineStats || {}), hostIds: ['mock-host'],
    currentHostId: 'mock-host', hosts: { 'mock-host': {} } },
};
`;
  fs.writeFileSync(configFile, body, { encoding: 'utf8', mode: 0o600 });
  return configFile;
}

module.exports = { getFreePort, writeDesktopConfig };
