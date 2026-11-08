'use strict';
const fs = require('node:fs');
const path = require('node:path');
const L = require('../../lib/design_library.cjs');
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=', 'base64');
const HTML = `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;background:#102019;color:#fff;font:20px sans-serif}button{font:inherit;padding:16px}#state{padding:20px}</style></head><body><h1>Fixture design</h1><button id="session">Session</button><button id="back">Back</button><div id="state">Chats</div><script>document.querySelector('#session').addEventListener('click',()=>{document.querySelector('#state').textContent='Session';localStorage.setItem('draft','temporary')});document.querySelector('#back').addEventListener('click',()=>{document.querySelector('#state').textContent='Chats'});</script></body></html>`;
function drop(root, { id = 'example', html = HTML, complete = true, approvalRef = null } = {}) {
  fs.mkdirSync(path.join(root, 'payload'), { recursive: true }); fs.writeFileSync(path.join(root, 'payload', 'index.html'), html);
  const descriptor = { schema: 'design-drop.v1', project_id: id, title: id === 'example' ? 'Example' : id, source_kind: 'local', source_id: 'fixture', source_revision: 'fixture-v1', exported_at: '2026-01-01T00:00:00Z', exporter: 'test fixture', complete, missing_paths: complete ? [] : ['missing.html'], files: L.inventory(path.join(root, 'payload')), views: [{ id: 'home', title: 'Home', entry: 'index.html', surface: 'web', viewport: { width: 800, height: 600 }, approval_ref: null }], flows: [] };
  descriptor.views[0].approval_ref = approvalRef;
  L.atomicJson(path.join(root, 'drop.json'), descriptor); return descriptor;
}
async function revision(root, options = {}) {
  const source = path.join(root, 'drop-' + (options.id || 'example')), descriptor = drop(source, options);
  if (options.mediaBytes) {
    descriptor.flows = [{ id: 'media', title: 'Synthetic transport fixture', entry: 'index.html', view_ids: ['home'], viewport: { width: 800, height: 600 }, steps: [{ action: 'assert', name: 'Fixture state', selector: '#state' }] }];
    L.atomicJson(path.join(source, 'drop.json'), descriptor);
  }
  const imported = L.importDrop({ drop: source, staging: path.join(root, 'staging') });
  const m = await require('../../lib/design_prepare.cjs').prepareRevision(imported.dir);
  fs.mkdirSync(path.join(imported.dir, 'screenshots')); fs.writeFileSync(path.join(imported.dir, 'screenshots', 'home.png'), PNG);
  m.views[0].screenshots = [{ name: 'Home', path: 'screenshots/home.png' }]; m.state = 'recorded';
  if (options.mediaBytes) {
    fs.mkdirSync(path.join(imported.dir, 'videos'));
    // Transport fixtures deliberately do not claim encoded recording proof.
    fs.writeFileSync(path.join(imported.dir, 'videos/large.mp4'), Buffer.alloc(options.mediaBytes, 42));
    fs.writeFileSync(path.join(imported.dir, 'videos/raw.webm'), 'synthetic transport fixture');
    const flow = m.flows[0]; Object.assign(flow, { video: 'videos/large.mp4', recording: 'videos/raw.webm', screenshots: m.views[0].screenshots, generated: { input_id: m.input_id, preparation_hash: L.sha256(L.canonical(L.profileIdentity(m.profile))), viewport_hash: L.sha256(L.canonical(flow.viewport)), flow_hash: L.sha256(L.canonical(flow.steps)) } });
  }
  L.atomicJson(path.join(imported.dir, 'manifest.json'), m); L.finalize(imported.dir);
  return { ...imported, manifest: L.readJson(path.join(imported.dir, 'manifest.json')) };
}
module.exports = { PNG, HTML, drop, revision };
