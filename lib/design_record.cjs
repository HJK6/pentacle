'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const { sha256, canonical, readJson, atomicJson, contained, viewport, profileIdentity, inputIdentity } = require('./design_library.cjs');

function launchOptions() {
  const executablePath = process.env.PENTACLE_CHROME || process.env.CHROME_BIN || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
  return { headless: true, chromiumSandbox: true, ...(executablePath ? { executablePath } : {}) };
}
function executableHash(command) {
  const file = command.includes(path.sep) ? command : execFileSync('which', [command], { encoding: 'utf8' }).trim();
  return sha256(fs.readFileSync(file));
}
async function captureProfile(browserVersion) {
  let browser;
  try {
    if (!browserVersion) { browser = await chromium.launch(launchOptions()); browserVersion = browser.version(); }
    const ffmpeg = process.env.PENTACLE_FFMPEG || 'ffmpeg';
    return { browser: browserVersion, browser_sha256: executableHash(launchOptions().executablePath || chromium.executablePath()), ffmpeg: execFileSync(ffmpeg, ['-version'], { encoding: 'utf8', timeout: 10000 }).split('\n')[0], ffmpeg_sha256: executableHash(ffmpeg) };
  } finally { if (browser) await browser.close(); }
}
async function openPrepared(browser, { html, size, videoDir }) {
  const context = await browser.newContext({ viewport: size, offline: true, serviceWorkers: 'block', ...(videoDir ? { recordVideo: { dir: videoDir, size } } : {}) });
  const page = await context.newPage(), requests = [], errors = [];
  page.on('pageerror', e => errors.push(e.stack || e.message));
  context.on('request', r => requests.push(r.url()));
  // Interception is an assertion aid, not the prototype's network boundary.
  // Prepared source/CSP must avoid every request even without this abort.
  await context.route('**/*', route => route.abort());
  await page.setContent('<!doctype html><html><head><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}iframe{width:100%;height:100%;border:0;display:block}</style></head><body><iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Offline design"></iframe></body></html>');
  await page.locator('iframe').evaluate((frame, source) => { frame.srcdoc = source; }, html);
  const frame = page.frameLocator('iframe');
  await frame.locator('body').waitFor();
  return { context, page, frame, requests, errors };
}
async function ready(session, selector = 'body') {
  assertClean(session);
  await session.frame.locator(selector).first().waitFor({ state: 'visible', timeout: 15000 });
  await session.frame.locator('body').evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(async image => { if (image.getAttribute('src')) await image.decode(); }));
  });
  if (session.requests.length) throw new Error('offline page attempted network: ' + session.requests[0]);
  if (session.errors.length) throw new Error('prototype error: ' + session.errors[0]);
}
function assertClean(session) {
  if (session.requests.length || session.errors.length) throw new Error('prototype validation failed: ' + [...session.requests, ...session.errors].join('; '));
}
function binding(manifest, item, browserVersion, size) {
  return { input_id: manifest.input_id, preparation_hash: sha256(canonical(profileIdentity(manifest.profile))), browser_hash: manifest.profile.capture.browser_sha256, browser: browserVersion, viewport_hash: sha256(canonical(size)), flow_hash: sha256(canonical(item.steps || [])) };
}
async function step(frame, action) {
  if (action.action === 'wait') return new Promise(resolve => setTimeout(resolve, action.ms));
  const locator = frame.locator(action.selector);
  if (action.action === 'click') await locator.click({ timeout: 10000 });
  else if (action.action === 'fill') await locator.fill(action.value, { timeout: 10000 });
  else {
    await locator.waitFor({ state: action.visible === false ? 'hidden' : 'visible', timeout: 10000 });
    if (action.text !== undefined) {
      const text = await locator.innerText();
      if (!text.includes(action.text)) throw new Error(`${action.name}: expected text ${action.text}`);
    }
    if (action.attribute) {
      if (await locator.getAttribute(action.attribute) !== action.value) throw new Error(`${action.name}: attribute assertion failed`);
    }
  }
}
function probeVideo(file, size) {
  const raw = execFileSync(process.env.PENTACLE_FFPROBE || 'ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,pix_fmt,width,height:format=duration', '-of', 'json', file], { encoding: 'utf8', timeout: 30000 });
  const info = JSON.parse(raw), video = info.streams?.[0];
  if (video?.codec_name !== 'h264' || video?.pix_fmt !== 'yuv420p' || video?.width !== size.width || video?.height !== size.height || !(Number(info.format?.duration) > 0)) throw new Error('recording codec/duration/dimensions invalid');
  return info;
}
async function recordRevision(dir, { out = dir } = {}) {
  const manifestFile = path.join(dir, 'manifest.json'), manifest = readJson(manifestFile);
  if (manifest.revision_id) throw new Error('immutable revision; import an editing copy instead');
  const browser = await chromium.launch(launchOptions());
  const browserVersion = browser.version();
  const receipts = [];
  try {
    manifest.profile.recorder_sha256 = sha256(fs.readFileSync(__filename));
    manifest.profile.playwright = require('playwright/package.json').version;
    manifest.profile.capture = await captureProfile(browserVersion);
    manifest.input_id = inputIdentity(manifest.provenance, manifest.profile);
    fs.mkdirSync(path.join(dir, 'screenshots'), { recursive: true });
    fs.mkdirSync(path.join(dir, 'videos'), { recursive: true });
    for (const view of manifest.views) {
      if (!view.prepared) continue;
      const size = viewport(view.viewport); let session;
      try {
        session = await openPrepared(browser, { html: fs.readFileSync(contained(dir, view.prepared), 'utf8'), size });
        await ready(session, view.ready_selector || 'body');
        // Two animation frames plus real settling time avoid a screenshot of
        // the initial React mount. Animation timing is never sped up.
        await session.frame.locator('body').evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const file = `screenshots/${view.id}.png`;
        await session.page.screenshot({ path: path.join(dir, file) });
        assertClean(session);
        view.screenshots = [{ name: view.title, path: file }];
        view.generated = binding(manifest, view, browserVersion, size);
        if (!view.adaptations?.length) view.compatibility.reason = 'Offline browser capture passed; no network requests or script errors.';
        receipts.push({ item: view.id, status: 'captured', requests: [], errors: [] });
      } catch (error) {
        delete view.prepared;
        if (!view.screenshots?.length) view.compatibility = { status: 'incomplete', reason: error.message, fallback: null };
        else view.compatibility = { status: 'screenshot/video-only', reason: error.message, fallback: view.screenshots[0].path };
        receipts.push({ item: view.id, status: 'incomplete', reason: error.message });
      } finally { if (session) await session.context.close(); }
    }
    for (const flow of manifest.flows) {
      const view = manifest.views.find(v => v.entry === flow.entry && v.prepared);
      if (!view) throw new Error(`${flow.id}: flow entry is not offline interactive`);
      const size = viewport(flow.viewport || view.viewport);
      if (size.width % 2 || size.height % 2) throw new Error(`${flow.id}: H.264 viewport dimensions must be even`);
      const rawDir = path.join(dir, 'videos', '.record-' + flow.id); fs.mkdirSync(rawDir, { recursive: true });
      const session = await openPrepared(browser, { html: fs.readFileSync(contained(dir, view.prepared), 'utf8'), size, videoDir: rawDir });
      const video = session.page.video(), screenshots = [], hashes = new Set();
      try {
        await ready(session, view.ready_selector || 'body');
        const initial = `screenshots/${flow.id}-00.png`;
        await session.page.screenshot({ path: path.join(dir, initial) });
        screenshots.push({ name: 'Initial state', path: initial }); hashes.add(sha256(fs.readFileSync(path.join(dir, initial))));
        for (const [index, action] of flow.steps.entries()) {
          await step(session.frame, action); await ready(session);
          if (action.action !== 'wait') {
            const file = `screenshots/${flow.id}-${String(index + 1).padStart(2, '0')}.png`;
            await session.page.screenshot({ path: path.join(dir, file) });
            screenshots.push({ name: action.name, path: file }); hashes.add(sha256(fs.readFileSync(path.join(dir, file))));
          }
        }
        assertClean(session);
        if (hashes.size < 2 || !flow.steps.some(s => s.action === 'assert')) throw new Error(`${flow.id}: no asserted visible state transition`);
        // Keep the final state visible in the real-time recording.
        await session.page.waitForTimeout(600);
      } finally { await session.context.close(); }
      const raw = await video.path(), recording = `videos/${flow.id}.webm`, mp4 = `videos/${flow.id}.mp4`;
      fs.renameSync(raw, path.join(dir, recording)); fs.rmdirSync(rawDir);
      execFileSync(process.env.PENTACLE_FFMPEG || 'ffmpeg', ['-nostdin', '-v', 'error', '-y', '-i', path.join(dir, recording), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', path.join(dir, mp4)], { timeout: 120000 });
      const info = probeVideo(path.join(dir, mp4), size);
      const samples = execFileSync(process.env.PENTACLE_FFMPEG || 'ffmpeg', ['-nostdin', '-v', 'error', '-i', path.join(dir, mp4), '-vf', 'fps=2,scale=64:64', '-f', 'framemd5', '-'], { encoding: 'utf8', timeout: 30000 });
      const frameHashes = new Set(samples.split('\n').filter(line => line && !line.startsWith('#')).map(line => line.split(',').at(-1).trim()));
      if (frameHashes.size < 2) throw new Error(`${flow.id}: video contains no observable frame changes`);
      Object.assign(flow, { video: mp4, recording, screenshots, generated: binding(manifest, flow, browserVersion, size), media_info: info, compatibility: { status: view.compatibility.status, reason: view.compatibility.reason } });
      receipts.push({ item: flow.id, status: 'recorded', distinct_sample_frames: frameHashes.size, requests: [], errors: [] });
    }
    manifest.state = 'recorded'; manifest.browser = { version: browserVersion, playwright: require('playwright/package.json').version };
    atomicJson(manifestFile, manifest);
    fs.mkdirSync(out, { recursive: true });
    if (out !== dir) atomicJson(path.join(out, 'recording-receipt.json'), { input_id: manifest.input_id, receipts });
    return manifest;
  } catch (error) {
    manifest.state = 'failed'; manifest.failure = error.message; atomicJson(manifestFile, manifest); throw error;
  } finally { await browser.close(); }
}
module.exports = { launchOptions, captureProfile, openPrepared, ready, assertClean, step, binding, probeVideo, recordRevision };
