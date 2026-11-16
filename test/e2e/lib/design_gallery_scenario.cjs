'use strict';
async function designGallery(ctx) {
  const { session, report, cdp, url, timeoutMs } = ctx;
  const { waitForValue } = require('./web_scenarios');
  const base = new URL(url).origin;
  try {
    for (const [width, height] of [[1440, 900], [390, 844], [375, 812]]) {
      await session.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 600 });
      await session.send('Page.navigate', { url: base + '/designs/' });
      await waitForValue(session, cdp, 'document.querySelectorAll(".dg-projects button").length', count => count === 3, { timeoutMs, label: 'three gallery projects' });
      for (const id of ['example', 'sample', 'demo']) {
        await session.eval(`document.querySelector('[data-project="${id}"]').click()`);
        report.ok(`gallery ${width}: project ${id}`, await session.eval(`document.querySelector('[data-project="${id}"]').getAttribute('aria-pressed')==='true'`));
      }
      const retainedId = await session.eval(`fetch('/api/designs').then(r=>r.json()).then(c=>c.projects.find(p=>p.id==='demo').revisions.find(r=>r.source_revision==='fixture-A').revision_id)`);
      await session.eval(`(()=>{const history=document.querySelector('[aria-label="History"]');history.value=${JSON.stringify(retainedId)};history.dispatchEvent(new Event('change'))})()`);
      await waitForValue(session, cdp, 'document.querySelector("h1")?.textContent', value => value === 'Retained A', { timeoutMs, label: 'retained history content' });
      await session.send('Page.reload');
      await waitForValue(session, cdp, 'document.querySelector("h1")?.textContent', value => value === 'Retained A', { timeoutMs, label: 'retained history restored after reload' });
      report.ok(`gallery ${width}: History and media agree after reload`, await session.eval(`document.querySelector('[aria-label="History"]').value===${JSON.stringify(retainedId)}`));
      await session.eval(`(()=>{const history=document.querySelector('[aria-label="History"]');history.value='';history.dispatchEvent(new Event('change'))})()`);
      await waitForValue(session, cdp, 'document.querySelector("h1")?.textContent', value => value === 'Current B', { timeoutMs, label: 'return to current history' });
      report.ok(`gallery ${width}: no horizontal overflow`, await session.eval('document.documentElement.scrollWidth<=window.innerWidth'));
      report.ok(`gallery ${width}: only live renders`, await session.eval('document.querySelectorAll(".dg-kinds button,.dg-stage img,.dg-stage video").length===0'));
      await waitForValue(session, cdp, 'document.querySelector(".dg-stage iframe")?.getAttribute("sandbox")', value => value === 'allow-scripts', { timeoutMs, label: 'opaque prototype' });
      report.ok(`gallery ${width}: opaque sandbox`, await session.eval('document.querySelector(".dg-stage iframe").getAttribute("referrerpolicy")==="no-referrer"'));
      await session.eval('[...document.querySelectorAll(".dg-modes button")].find(b=>b.textContent==="Flow overview").click()');
      await waitForValue(session, cdp, 'document.querySelectorAll(".dg-overview iframe").length', value => value === 1, { timeoutMs, label: 'live overview fallback' });
      report.ok(`gallery ${width}: viewport-sized overview`, await session.eval('document.querySelector(".dg-flow-card").getBoundingClientRect().width===800 && document.querySelector(".dg-flow-preview").getBoundingClientRect().height===600'));
      report.ok(`gallery ${width}: overview scroll contained`, await session.eval('document.documentElement.scrollWidth<=innerWidth'));
      await session.screenshot(require('node:path').join(report.dir, `design-gallery-${width}x${height}.png`));
      await session.send('Page.reload');
      await waitForValue(session, cdp, 'document.querySelector("[data-item=home]")?.getAttribute("aria-pressed")', value => value === 'true', { timeoutMs, label: 'gallery selection survives reload' });
    }
  } finally {
    await session.send('Emulation.clearDeviceMetricsOverride'); await session.send('Page.navigate', { url });
    await waitForValue(session, cdp, '!!window.cc && !!document.getElementById("session-list")', value => value === true, { timeoutMs, label: 'restore main app' });
  }
}
module.exports = { designGallery };
