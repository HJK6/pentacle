'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const hostPresentation = require('../renderer/host_presentation');
const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
const start = source.indexOf('function machineSigilMarkup(');
const code = source.slice(start, source.indexOf('\n}', start) + 2);
for (const count of [1, 2]) for (const condition of [true, false]) {
  test(`${count} host(s), single-host condition ${condition}: icons ${count === 1 && condition ? 'absent' : 'present'}`, () => {
    const roster = ['laptop', 'server'].slice(0, count);
    const visible = hostPresentation.showMachineIcons({}, roster, [], condition);
    const context = { window: {}, CONFIG: {}, HOST_IDS: roster, hostPresentation,
      showMachineIcons: () => visible, esc: String, getSourceInitial: hostPresentation.initial };
    vm.runInNewContext(code, context);
    const markup = context.machineSigilMarkup('laptop', 'Laptop');
    assert.equal(markup, count === 1 && condition ? '' : 'L');
    // Actual SVG renderer follows the same guard as the fallback initial.
    context.window.PentacleCosmic = { machineSigil: () => ({ setAttribute() {}, classList: { add() {} }, outerHTML: '<svg class="machine-sigil"></svg>' }) };
    context.CONFIG = { hostColors: { laptop: 'royal-blue' } };
    assert.equal(context.machineSigilMarkup('laptop', 'Laptop').includes('<svg'), !(count === 1 && condition));
    assert.ok(context.machineSigilMarkup('laptop', 'Assistant', 15, 'djinni').includes('<svg'), 'assistant artwork remains');
  });
}
test('canonical aliases count once; a discovered second host restores icons', () => {
  const config = { chatStream: { localHost: 'laptop', hostMap: { local: 'laptop' } } };
  assert.equal(hostPresentation.showMachineIcons(config, ['local', 'laptop']), false);
  assert.equal(hostPresentation.showMachineIcons(config, ['local'], ['server']), true);
  assert.equal(hostPresentation.showMachineIcons(config, []), true, 'unknown fleet is not exactly one');
});

for (const count of [1, 2]) {
  test(`${count} host(s): source filter retains All and labeled, working host controls`, () => {
    const { JSDOM } = require('jsdom');
    const document = new JSDOM('<div id="source-filter-bar"></div>').window.document;
    const roster = ['laptop', 'server'].slice(0, count);
    const names = { laptop: 'Laptop & desk', server: 'Server' };
    const state = { sessions: [], sourceFilter: null };
    let renders = 0;
    const context = { document, state, HOST_IDS: roster,
      collectSourceFilterHostIds: () => roster,
      getSourceForSession: (_, id) => names[id], getSourceColorForSession: () => 'blue',
      showMachineIcons: () => count > 1,
      machineSigilMarkup: () => count > 1 ? '<svg class="machine-sigil"></svg>' : '',
      esc: value => String(value).replaceAll('&', '&amp;'), renderSidebar: () => { renders++; } };
    const begin = source.indexOf('function renderSourceFilterBar(');
    vm.runInNewContext(source.slice(begin, source.indexOf('\n}', begin) + 2), context);
    context.renderSourceFilterBar([]);
    const bar = document.getElementById('source-filter-bar');
    assert.equal(bar.style.display, 'flex');
    assert.equal(bar.querySelectorAll('.machine-sigil').length, count === 1 ? 0 : 2);
    const all = bar.querySelector('[data-host="all"]');
    assert.equal(all.textContent, 'All');
    for (const id of roster) {
      const button = bar.querySelector(`[data-host="${id}"]`);
      assert.equal(button.title, names[id]);
      assert.equal(button.getAttribute('aria-label'), names[id]);
      assert.equal(button.textContent, count === 1 ? names[id] : '');
      button.click();
      assert.equal(state.sourceFilter, id);
    }
    all.click();
    assert.equal(state.sourceFilter, null);
    assert.equal(renders, count + 1);
  });
}
