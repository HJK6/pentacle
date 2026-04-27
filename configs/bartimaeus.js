'use strict';

module.exports = {
  ...require('../pentacle.config.example.js'),
  appName: 'Pentacle',
  appId: 'com.pentacle.bartimaeus',
  workingDirectory: '~/agent-workspace',
  agents: {
    codex: {
      label: 'Codex',
      command: '/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox',
    },
  },
  features: {
    mic: true,
    usage: true,
    botsTab: true,
    inputBar: true,
    dashboards: true,
    sourceTags: true,
  },
  chatStream: {
    url: 'ws://127.0.0.1:7791',
    autoStart: true,
    hosts: ['bart', 'merlin', 'amaterasu'],
    recentLimit: 5000,
  },
  peers: [
    {
      id: 'merlin',
      host: '100.70.128.35',
      user: 'vgujju',
      port: 22,
      tmux: '/opt/homebrew/bin/tmux',
    },
    {
      id: 'amaterasu',
      host: '100.104.128.92',
      user: 'vamsh',
      port: 22,
      tmux: '/usr/bin/tmux',
    },
  ],
  hostNames: {
    local: 'Bartimaeus',
    merlin: 'Merlin',
    amaterasu: 'Amaterasu',
  },
  hostColors: {
    local: 'forest-green',
    merlin: 'royal-blue',
    amaterasu: 'red',
  },
  machineStats: {
    hostIds: ['local', 'merlin', 'amaterasu'],
    hosts: {
      local: {},
      merlin: {},
      amaterasu: {},
    },
  },
};
