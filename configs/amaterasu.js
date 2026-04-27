'use strict';

module.exports = {
  ...require('../pentacle.config.example.js'),
  appName: 'Pentacle',
  appId: 'com.pentacle.amaterasu',
  remote: {
    host: '100.80.28.24',
    user: 'bartimaeus',
    tmux: '/opt/homebrew/bin/tmux',
    apiPort: 7778,
    port: 22,
  },
  localWsl: {
    distro: 'Ubuntu',
    sshPort: 22,
    user: 'vamsh',
    tmux: '/usr/bin/tmux',
  },
  peers: [
    {
      id: 'merlin',
      host: '100.70.128.35',
      user: 'vgujju',
      port: 22,
      tmux: '/opt/homebrew/bin/tmux',
    },
  ],
  workingDirectory: '~/agent-workspace',
  agents: {
    codex: {
      label: 'Codex',
      command: '/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox',
      commandLocal: 'codex --dangerously-bypass-approvals-and-sandbox',
    },
  },
  features: {
    mic: false,
    usage: true,
    botsTab: true,
    inputBar: true,
    dashboards: true,
    sourceTags: true,
  },
  chatStream: {
    url: 'ws://100.80.28.24:7791',
    autoStart: false,
    recentLimit: 5000,
  },
  hostNames: {
    local: 'Amaterasu',
    remote: 'Bartimaeus',
    merlin: 'Merlin',
  },
  hostColors: {
    local: 'red',
    remote: 'forest-green',
    merlin: 'royal-blue',
  },
  machineStats: {
    hostIds: ['local', 'remote', 'merlin'],
    hosts: {
      local: {},
      remote: {},
      merlin: {},
    },
  },
};
