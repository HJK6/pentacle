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
      report.ok(`gallery ${width}: no horizontal overflow`, await session.eval('document.documentElement.scrollWidth<=window.innerWidth'));
      report.ok(`gallery ${width}: modes usable`, await session.eval('[...document.querySelectorAll(".dg-modes button")].every(b=>{const r=b.getBoundingClientRect();return r.width>=40&&r.left>=0&&r.right<=innerWidth})'));
      await session.eval('[...document.querySelectorAll(".dg-kinds button")].find(b=>b.textContent==="Flows").click()');
      for (const id of ['open-back', 'working-question']) {
        await session.eval(`document.querySelector('[data-item="${id}"]').click()`);
        await session.eval('[...document.querySelectorAll(".dg-modes button")].find(b=>b.textContent==="Flow video").click()');
        await waitForValue(session, cdp, 'document.querySelector("video")?.readyState', value => value >= 1, { timeoutMs, label: 'playable flow video' });
        await session.eval('document.querySelector("video").currentTime=1');
        await waitForValue(session, cdp, 'document.querySelector("video")?.currentTime', value => value >= 0.9, { timeoutMs, label: 'video seek' });
        await session.eval('[...document.querySelectorAll(".dg-modes button")].find(b=>b.textContent==="Screens").click()');
        report.ok(`gallery ${width}: video cleaned on mode change`, await session.eval('!document.querySelector("video")'));
      }
      await session.eval('[...document.querySelectorAll(".dg-modes button")].find(b=>b.textContent==="Prototype").click()');
      await waitForValue(session, cdp, 'document.querySelector(".dg-stage iframe")?.getAttribute("sandbox")', value => value === 'allow-scripts', { timeoutMs, label: 'opaque prototype' });
      report.ok(`gallery ${width}: opaque sandbox`, await session.eval('document.querySelector(".dg-stage iframe").getAttribute("referrerpolicy")==="no-referrer"'));
      await session.screenshot(require('node:path').join(report.dir, `design-gallery-${width}x${height}.png`));
      await session.send('Page.reload');
      await waitForValue(session, cdp, 'document.querySelector("[data-item=working-question]")?.getAttribute("aria-pressed")', value => value === 'true', { timeoutMs, label: 'gallery selection survives reload' });
    }
  } finally {
    await session.send('Emulation.clearDeviceMetricsOverride'); await session.send('Page.navigate', { url });
    await waitForValue(session, cdp, '!!window.cc && !!document.getElementById("session-list")', value => value === true, { timeoutMs, label: 'restore main app' });
  }
}
module.exports = { designGallery };
