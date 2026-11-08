'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const L = require('../../../lib/design_library.cjs');
const P = require('../../../lib/design_prepare.cjs');
const { drop, PNG, HTML } = require('../../fixtures/design_library.cjs');

// Public synthetic media exercises HTTP/video controls. Real export recording
// acceptance uses design_library_probe and the private named-flow receipts.
async function buildDesignGalleryFixture(scratch) {
  const library = path.join(scratch, 'design-library'), source = path.join(scratch, 'design-inputs');
  fs.mkdirSync(source, { recursive: true });
  const video = path.join(source, 'fixture.mp4');
  execFileSync(process.env.PENTACLE_FFMPEG || 'ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=12', '-t', '2', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', video], { timeout: 30000 });
  for (const id of ['example', 'sample', 'demo']) {
    for (const generation of ['A', 'B']) {
    const dropRoot = path.join(source, id + '-' + generation), descriptor = drop(dropRoot, { id, html: HTML.replace('Fixture design', 'Fixture generation ' + generation) });
    descriptor.source_revision = 'fixture-' + generation;
    descriptor.views[0].title = generation === 'A' ? 'Retained A' : 'Current B';
    descriptor.flows = ['open-back', 'working-question'].map(name => ({ id: name, title: 'Fixture ' + name, entry: 'index.html', view_ids: ['home'], viewport: { width: 800, height: 600 }, steps: [{ action: 'click', name: 'Session', selector: '#session' }, { action: 'assert', name: 'Session visible', selector: '#state', text: 'Session' }] }));
    L.atomicJson(path.join(dropRoot, 'drop.json'), descriptor);
    const imported = L.importDrop({ drop: dropRoot, staging: path.join(source, 'staging') }), m = await P.prepareRevision(imported.dir);
    fs.mkdirSync(path.join(imported.dir, 'screenshots')); fs.writeFileSync(path.join(imported.dir, 'screenshots/home.png'), PNG);
    fs.mkdirSync(path.join(imported.dir, 'videos')); fs.copyFileSync(video, path.join(imported.dir, 'videos/fixture.mp4')); fs.writeFileSync(path.join(imported.dir, 'videos/fixture.webm'), 'synthetic retained recording fixture');
    m.views[0].screenshots = [{ name: 'Synthetic reference', path: 'screenshots/home.png' }];
    for (const flow of m.flows) Object.assign(flow, { compatibility: { status: 'offline-with-mocks/limitations', reason: 'Synthetic media fixture for browser controls' }, video: 'videos/fixture.mp4', recording: 'videos/fixture.webm', screenshots: m.views[0].screenshots, generated: { input_id: m.input_id, preparation_hash: L.sha256(L.canonical(L.profileIdentity(m.profile))), browser_hash: L.sha256('synthetic fixture'), viewport_hash: L.sha256(L.canonical(flow.viewport)), flow_hash: L.sha256(L.canonical(flow.steps)) } });
    m.state = 'recorded'; L.atomicJson(path.join(imported.dir, 'manifest.json'), m); L.finalize(imported.dir); L.publishRevision({ dir: imported.dir, library });
    }
  }
  return library;
}
module.exports = { buildDesignGalleryFixture };
