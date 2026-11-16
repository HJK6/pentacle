const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

test('PWA icon artwork changes also change its manifest URL', () => {
  const renderer = path.join(__dirname, '..', 'renderer');
  const manifest = JSON.parse(fs.readFileSync(path.join(renderer, 'manifest.webmanifest'), 'utf8'));
  assert.ok(manifest.icons.length > 0);
  for (const icon of manifest.icons) {
    const bytes = fs.readFileSync(path.join(renderer, icon.src));
    const digest = crypto.createHash('sha256').update(bytes).digest('hex').slice(0, 12);
    // Chrome can skip icon downloads when manifest metadata has not changed.
    assert.ok(icon.src.endsWith(`.${digest}.png`),
      `${icon.src}: use the artwork digest in the filename so installed apps detect changes`);
  }
});
