'use strict';
const fixture = require('../../../pentacle-chat-core/tests/fixtures/summary-fallback-frames.json');
const fs = require('node:fs');
const path = require('node:path');
const { reloadDashboardPage } = require('./dashboard_scenario');

// Real provider-ingested wire fixture, replayed through the served store and
// renderer. MutationObserver records intermediate DOM, including working ticks.
async function summaryFallbackFormatting(ctx) {
  const { session, report, cdp, timeoutMs } = ctx;
  await reloadDashboardPage(ctx);
  await session.waitFor('!!window.PentacleChatStore && !!window.PentacleChatView', { timeoutMs });
  let observation;
  try {
    observation = await session.eval(`(async () => {
      const fixture = ${JSON.stringify(fixture)};
      const stream = fixture.session.stream_id;
      const store = window.PentacleChatStore;
      const container = document.createElement('div');
      container.style.cssText = 'position:fixed;inset:20px;width:800px;overflow:auto;z-index:100000;background:white;color:black';
      document.body.appendChild(container);
      let phase = 'before-backfill';
      const mutations = [];
      const sample = () => ({ phase, rows: container.querySelectorAll('.slot-chat-row').length,
        headings: [...container.querySelectorAll('.slot-chat-md-heading')].map(node => node.textContent),
        listItems: [...container.querySelectorAll('.slot-chat-md-list li')].map(node => node.textContent),
        code: container.querySelector('.slot-chat-md-code code')?.textContent || '',
        text: container.textContent,
        fallbacks: store.selectSessionDetail(stream, {visibleCount:'all'}).transcriptItems.filter(item => item.eventKey === 'uncorrelated_fallback').length });
      const observer = new MutationObserver(() => mutations.push(sample()));
      observer.observe(container, {childList:true, subtree:true, characterData:true});
      const render = async (events, working) => {
        store.applyFrame({type:'snapshot', sessions:[{...fixture.session, working}], events, drafts:{}});
        window.PentacleChatView.renderStreamTranscript(stream, container, {visibleCount:'all'});
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return sample();
      };
      try {
        const before = await render([], false);
        phase = 'held-transcript';
        const held = [];
        for (const working of [false, true, false, true, false]) held.push(await render(fixture.events, working));
        return { before, held, mutations };
      } finally { observer.disconnect(); container.remove(); }
    })()`);
    fs.writeFileSync(path.join(report.dir, 'summary-fallback-observation.json'), JSON.stringify(observation, null, 2));
    const formatted = value => value.rows === 1
      && JSON.stringify(value.headings) === JSON.stringify(['Summary fallback probe'])
      && JSON.stringify(value.listItems) === JSON.stringify(['First item', 'Second item'])
      && value.code === 'const first = 1;\n  const second = 2;';
    // Record both outcomes before throwing, so one symptom cannot mask the other.
    const formattingPass = formatted(observation.before) && observation.held.every(formatted);
    const heldMutations = observation.mutations.filter(value => value.phase === 'held-transcript');
    const flickerPass = heldMutations.length >= 3
      && heldMutations.every(value => value.rows === 1 && value.fallbacks === 0)
      && observation.held.every(value => value.rows === 1 && value.fallbacks === 0);
    fs.writeFileSync(path.join(report.dir, 'summary-fallback-predicates.json'), JSON.stringify({ formattingPass, flickerPass,
      classification: formattingPass && flickerPass ? 'PASS' : 'PRODUCT_FAIL' }, null, 2));
    report.ok('summary fallback keeps multiline Markdown and code before and after backfill', formattingPass, observation);
    report.ok('no transient fallback mounts while the matching transcript event is held', flickerPass, observation);
  } finally {
    await reloadDashboardPage(ctx);
  }
}

module.exports = { summaryFallbackFormatting };
