'use strict';

const { main } = require('../../../server/index');

async function run() {
  const token = process.argv[3];
  const mode = token ? ['--token-file', token] : [
    '--auth', 'tailscale', '--allow-login', 'tester@example.test',
    '--origin', 'https://pentacle.example.ts.net',
  ];
  const host = await main(['--profile', process.argv[2], '--port', '0', ...mode]);
  async function stop() {
    await host.close();
    process.exit(0);
  }
  process.once('disconnect', stop);
  process.once('SIGTERM', stop);
  process.send({ port: host.port });
}

run().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
  process.disconnect();
});
