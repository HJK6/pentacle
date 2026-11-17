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

// HTML consumers have their own icon cache; each must select the same version.
test('favicon and touch icons are versioned consistently across web windows', () => {
  const renderer = path.join(__dirname, '..', 'renderer');
  const main = fs.readFileSync(path.join(renderer, 'index.html'), 'utf8');
  for (const file of ['index.html', 'asset_popout.html', 'designs.html']) {
    const html = fs.readFileSync(path.join(renderer, file), 'utf8');
    const links = [...html.matchAll(/<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"\n]+)"/g)];
    assert.ok(links.length > 0, `${file} declares an icon`);
    for (const [, src] of links) {
      const relative = src.replace(/^\//, '');
      assert.ok(main.includes(relative), `${file} uses the main page's icon version`);
      const bytes = fs.readFileSync(path.join(renderer, relative));
      const digest = crypto.createHash('sha256').update(bytes).digest('hex').slice(0, 12);
      assert.ok(src.endsWith(`.${digest}.png`), `${file}: version the icon URL`);
    }
  }
});
