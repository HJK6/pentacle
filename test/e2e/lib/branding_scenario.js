'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '../../..');
const MASTER = '6f6533d7585b3532e63ec8226b218e3c47658d212734e3c9ff181f8be7c35117';
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

// Real browser decoding/network bytes and Chrome's selected favicon metadata.
// Installed Windows app/shortcut caches require their separate release proof.
async function branding(ctx) {
  const { session, report, cdp, cdpPort, url } = ctx;
  const observations = { assets: [], layouts: [], auxiliary: [], fallbackRequests: [] };
  const before = await session.eval(`({theme: document.documentElement.dataset.theme,
    style: document.documentElement.classList.contains('chat-first-style'),
    refreshHidden: document.getElementById('web-refresh-btn').hidden})`);
  const network = raw => {
    const message = JSON.parse(String(raw));
    if (message.method === 'Network.requestWillBeSent' && /\/favicon\.ico(?:\?|$)/.test(message.params.request.url)) {
      observations.fallbackRequests.push(message.params.request.url);
    }
  };
  session.ws.on('message', network);
  await session.send('Network.enable');
  try {
    report.ok('approved branding master is unchanged', sha(fs.readFileSync(path.join(ROOT, 'renderer/assets/pentacle-logo-source.png'))) === MASTER);
    const links = await session.eval(`[...document.querySelectorAll('link[rel="icon"],link[rel="apple-touch-icon"],link[rel="manifest"]')].map(l => ({rel:l.rel,url:l.href}))`);
    report.ok('page declares favicon, touch icon and manifest', ['icon', 'apple-touch-icon', 'manifest'].every(rel => links.some(l => l.rel === rel)));
    const manifestLink = links.find(l => l.rel === 'manifest');
    const manifest = await session.eval(`fetch(${JSON.stringify(manifestLink.url)}).then(r => r.json())`);
    report.ok('manifest preserves app identity and all four icon purposes', manifest.name === 'Pentacle' && manifest.short_name === 'Pentacle'
      && manifest.scope === '/' && manifest.start_url === '/' && manifest.icons.length === 4
      && manifest.icons.filter(i => i.purpose === 'maskable').length === 2, manifest);
    const resources = [...links, ...manifest.icons.map(icon => ({ ...icon, url: new URL(icon.src, manifestLink.url).href }))];
    for (const resource of resources) {
      const fetched = await session.eval(`(async () => {
        const response = await fetch(${JSON.stringify(resource.url)});
        const blob = await response.blob();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let dimensions = null;
        if (blob.type === 'image/png') { const image = await createImageBitmap(blob); dimensions = [image.width, image.height]; image.close(); }
        return {status:response.status,mime:blob.type,bytes:Array.from(bytes),dimensions};
      })()`);
      const relative = new URL(resource.url).pathname.slice(1);
      const source = fs.readFileSync(path.join(ROOT, 'renderer', relative));
      const built = fs.readFileSync(path.join(ROOT, 'renderer/dist/web', relative));
      const record = { ...resource, status: fetched.status, mime: fetched.mime, dimensions: fetched.dimensions,
        sourceSha256: sha(source), builtSha256: sha(built), browserSha256: sha(Buffer.from(fetched.bytes)) };
      observations.assets.push(record);
      report.ok(`browser bytes match source and build: ${relative}`, record.status === 200 && record.sourceSha256 === record.builtSha256 && record.builtSha256 === record.browserSha256, record);
      if (resource.rel === 'manifest') report.ok('manifest MIME is correct', fetched.mime === 'application/manifest+json');
      else {
        const expected = resource.sizes ? resource.sizes.split('x').map(Number) : resource.rel === 'icon' ? [64, 64] : [180, 180];
        report.ok(`browser decodes declared PNG dimensions: ${relative}`, fetched.mime === 'image/png' && JSON.stringify(fetched.dimensions) === JSON.stringify(expected), record);
      }
    }
    observations.manifest = await session.send('Page.getAppManifest');
    report.ok('Chrome parses the selected manifest without errors', observations.manifest.url === manifestLink.url && observations.manifest.errors.length === 0, observations.manifest);
    const favicon = links.find(l => l.rel === 'icon').url;
    const selectedFavicon = async targetSession => {
      let target;
      for (let i = 0; i < 30; i++) {
        target = (await cdp.listTargets(cdpPort)).find(t => t.id === targetSession.target.id);
        if (target?.faviconUrl === favicon) break;
        await cdp.sleep(100);
      }
      return target?.faviconUrl;
    };
    observations.faviconUrl = await selectedFavicon(session);
    report.ok('Chrome selects the approved favicon URL', observations.faviconUrl === favicon, observations.faviconUrl);

    // Check every HTML surface actually shipped by this web build. Third-party
    // dashboard pages are external content and do not inherit app branding.
    const auxiliary = fs.readdirSync(path.join(ROOT, 'renderer/dist/web')).filter(f => f.endsWith('.html') && f !== 'web.html');
    for (const file of auxiliary) {
      const { targetId } = await session.send('Target.createTarget', { url: new URL(file, url).href });
      let page;
      try {
        page = await cdp.connect(cdpPort, { match: new RegExp(file.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) });
        await page.waitFor('document.readyState === "complete"');
        const declared = await page.eval(`document.querySelector('link[rel="icon"]')?.href`);
        const selected = await selectedFavicon(page);
        observations.auxiliary.push({ file, declared, selected });
        report.ok(`auxiliary window selects approved favicon: ${file}`, declared === favicon && selected === favicon, observations.auxiliary.at(-1));
      } finally { page?.close(); await session.send('Target.closeTarget', { targetId }); }
    }

    for (const style of [false, true]) for (const theme of ['dark', 'light']) for (const width of [360, 1600]) {
      await session.send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false });
      await session.eval(`document.documentElement.classList.toggle('chat-first-style',${style});
        document.querySelector('[data-setting="theme"] [data-value="${theme}"]').click();
        document.getElementById('web-refresh-btn').hidden=false`);
      await cdp.sleep(200);
      const layout = await session.eval(`(() => {
        const wordmark=document.querySelector('.titlebar .pentacle-wordmark');
        const box=el=>{if(!el)return null;const r=el.getBoundingClientRect(),s=getComputedStyle(el);return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height,
          visible:s.display!=='none'&&s.visibility!=='hidden'&&+s.opacity>0&&r.width>0&&r.height>0};};
        const settings=document.getElementById('settings-btn'),refresh=document.getElementById('web-refresh-btn');
        const hit=el=>{const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));};
        return {text:wordmark?.innerText,wordmark:box(wordmark),header:box(document.querySelector('.titlebar')),image:!!document.querySelector('.titlebar img'),
          settings:box(settings),refresh:box(refresh),settingsHit:hit(settings),refreshHit:hit(refresh),viewport:innerWidth};
      })()`);
      observations.layouts.push({ style, theme, width, ...layout });
      const w = layout.wordmark;
      report.ok(`visible Pentacle wordmark and reachable controls (${style ? 'chat-first' : 'classic'},${theme},${width})`, layout.text === 'Pentacle' && !layout.image && w?.visible
        && w.x >= 0 && w.right <= layout.settings.x && w.bottom <= layout.header.bottom
        && layout.settings.visible && layout.refresh.visible && layout.settingsHit && layout.refreshHit
        && layout.refresh.right <= width, layout);
      await session.click('#settings-btn');
      report.ok('settings opens from the branded header', await session.eval(`getComputedStyle(document.getElementById('settings-overlay')).display !== 'none'`));
      await session.click('#settings-close');
      await session.screenshot(path.join(report.dir, `branding-${style ? 'chat-first' : 'classic'}-${theme}-${width}.png`));
    }
    report.ok('declared icons avoid the browser favicon.ico fallback', observations.fallbackRequests.length === 0, observations.fallbackRequests);
  } finally {
    session.ws.off('message', network);
    fs.writeFileSync(path.join(report.dir, 'branding-observations.json'), JSON.stringify(observations, null, 2));
    await session.send('Emulation.clearDeviceMetricsOverride');
    await session.eval(`document.documentElement.classList.toggle('chat-first-style',${before.style});
      document.querySelector('[data-setting="theme"] [data-value="${before.theme}"]').click();
      document.getElementById('web-refresh-btn').hidden=${before.refreshHidden};
      document.getElementById('settings-close').click()`);
  }
}

module.exports = { branding };
