const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '..');

test('desktop config reference and example cover every directly read top-level key', () => {
  const files = ['renderer/app.js', 'main.js', 'preload.js', 'hosts.js', ...fs.readdirSync(path.join(root, 'main')).filter(n => n.endsWith('.js')).map(n => `main/${n}`)];
  const keys = new Set();
  for (const file of files) {
    const source = fs.readFileSync(path.join(root, file), 'utf8');
    for (const match of source.matchAll(/\b(?:CONFIG|config|cfg|_cfg)(?:\?\.|\.)([A-Za-z_$][\w$]*)/g)) keys.add(match[1]);
  }
  const docs = fs.readFileSync(path.join(root, 'docs/desktop_config.md'), 'utf8');
  const example = fs.readFileSync(path.join(root, 'pentacle.config.example.js'), 'utf8');
  assert.ok(keys.size > 15, 'extraction must find the desktop config population');
  for (const key of keys) {
    assert.ok(docs.includes(`\`${key}\``), `reference missing ${key}`);
    assert.ok(new RegExp(`\\b${key}\\b`).test(example), `example missing ${key}`);
  }
});

test('desktop authored production files contain no anonymized identity residue', () => {
  const files = ['main.js', 'preload.js', 'config-loader.js', 'hosts.js', 'pentacle.config.example.js', 'README.md', 'SETUP.md', 'AGENT_SETUP.md', 'docs/desktop_config.md'];
  function collect(dir) {
    for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
      if (entry.name === 'dist') continue; // generated bundle contains separately owned chat-core
      const relative = `${dir}/${entry.name}`;
      if (entry.isDirectory()) collect(relative);
      else if (/\.(?:js|ts|css|html|md)$/.test(entry.name)) files.push(relative);
    }
  }
  collect('renderer'); collect('main');
  const remaining = files.filter(file => /hosta|hostb|hostc|abra/.test(fs.readFileSync(path.join(root, file), 'utf8')));
  assert.deepEqual(remaining, []);
});
