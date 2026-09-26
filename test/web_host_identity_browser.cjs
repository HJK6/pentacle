#!/usr/bin/env node
'use strict';

// Read-only browser gate for the host identity surfaces. The live URL supplies
// configured hosts/sessions; local built assets are overlaid unless disabled.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require(process.env.PENTACLE_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'renderer/dist/web');
const output = path.resolve(process.env.PENTACLE_IDENTITY_OUTPUT || '');
const configPath = process.env.PENTACLE_IDENTITY_CONFIG;
if (!configPath || !path.isAbsolute(configPath)) throw Error('External identity config path is required');
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const url = config.url;
const assistant = config.assistant;
const ordinary = config.ordinary;
const assistantStreamId = assistant?.streamId;
const ordinaryStreamId = ordinary?.streamId;
const overlay = process.env.PENTACLE_IDENTITY_OVERLAY !== '0';
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const identityAssets = ['bundle.js', 'chat_core.bundle.js', 'styles.css', 'chat_v3.css',
  'cosmic_theme.css', 'cosmic_chat_surface.css', 'cosmic_tokens.bundle.js', 'cosmic_components.bundle.js'];
const expectedServed = !overlay && config.expectedServedAssets
  ? JSON.parse(fs.readFileSync(config.expectedServedAssets, 'utf8')) : null;
const expected = config.hosts || {};
const sharedMemoryRoot = config.sharedMemoryRoot && path.resolve(config.sharedMemoryRoot);
if (!url || !assistantStreamId || !ordinaryStreamId || !process.env.PENTACLE_IDENTITY_OUTPUT) {
  throw Error('Identity URL, assistant/ordinary stream IDs and external output path are required');
}
if (output === root || output.startsWith(`${root}${path.sep}`)) throw Error('Identity output must be outside the source checkout');
if (configPath === root || configPath.startsWith(`${root}${path.sep}`)) throw Error('Identity config must be outside the source checkout');
if (!sharedMemoryRoot) throw Error('Shared memory root is required for private-output guard');
if (output === sharedMemoryRoot || output.startsWith(`${sharedMemoryRoot}${path.sep}`)) throw Error('Identity output must be outside shared memory');
if (configPath === sharedMemoryRoot || configPath.startsWith(`${sharedMemoryRoot}${path.sep}`)) throw Error('Identity config must be outside shared memory');
if (Object.keys(expected).length < 2 || assistantStreamId === ordinaryStreamId) throw Error('Distinct assistant/ordinary streams and host expectations are required');
if (!overlay && (!expectedServed || !config.expectedBuildId)) throw Error('Served mode requires expected asset manifest and build ID');
fs.mkdirSync(output, { recursive: true });

function assertMachineIdentity(machine, spec, glyphs, surface) {
  assert(machine, `${surface} missing ${spec.label}`);
  assert.equal(machine.tagName, 'BUTTON', `${surface} ${spec.label} native button`);
  assert.equal(machine.svg, true, `${surface} ${spec.label} SVG`);
  assert.equal(machine.markText, '', `${surface} ${spec.label} legacy initial`);
  assert(machine.label.includes(spec.label), `${surface} ${spec.label} accessible name`);
  assert.equal(machine.visibleLabel, spec.label, `${surface} ${spec.label} visible name`);
  assert(machine.className.includes(spec.color), `${surface} ${spec.label} colour token`);
  assert.equal(machine.glyphSignature, glyphs[spec.glyph], `${surface} ${spec.label} canonical glyph`);
  assert(machine.contrast >= 3, `${surface} ${spec.label} sigil contrast ${machine.contrast}`);
  assert(machine.labelContrast >= 4.5, `${surface} ${spec.label} name contrast ${machine.labelContrast}`);
}

function assertSlotIdentity(transition, streamId, glyphs) {
  assert.equal(transition.heroCount, 0, `no transcript hero after ${streamId || 'detach'}`);
  assert.equal(transition.identityOrnaments, 0, `no duplicate transcript ornament after ${streamId || 'detach'}`);
  if (streamId === assistantStreamId) {
    assert.equal(transition.label, assistant.displayName);
    assert.equal(transition.lampLabel, assistant.iconLabel);
    assert.equal(transition.lampSignature, glyphs.djinni);
    assert.equal(transition.host, assistant.hostLabel, 'actual host tag remains visible');
    assert(transition.lampClass.includes('color-forest-green'), 'assistant lamp uses green identity class');
    assert.equal(transition.lampColor, transition.expectedGreen, 'assistant lamp uses theme-correct green token');
    assert.equal(transition.provider, undefined, 'composite assistant has no invented provider tag');
    assert(transition.transcriptRows > 0, 'populated assistant transcript');
  } else if (streamId) {
    assert.equal(transition.label, ordinary.displayName);
    assert.equal(transition.host, ordinary.hostLabel);
    assert.equal(transition.lampLabel, undefined, 'no stale assistant lamp after same-slot replacement');
    assert(transition.provider, 'ordinary provider tag remains visible');
    assert(transition.transcriptRows > 0, 'populated ordinary transcript');
  } else {
    assert.match(transition.label || '', /Slot 1/i);
    assert.equal(transition.lampLabel, undefined, 'no lamp on detached slot');
  }
}

function assertObservedAssets(observed, expectedAssets) {
  for (const name of identityAssets) {
    assert.equal(observed[`/${name}`], expectedAssets[name], `${name} exact loaded asset`);
  }
}

async function inspect(page) {
  return page.evaluate(({ assistantStreamId, ordinaryStreamId }) => {
    const rgb = value => (String(value).match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = value => {
      const [r, g, b] = rgb(value).map(n => { n /= 255; return n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4; });
      return r * .2126 + g * .7152 + b * .0722;
    };
    const ratio = (foreground, background) => {
      const a = luminance(foreground), b = luminance(background);
      return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    };
    const identity = (node, markSelector) => {
      const mark = markSelector === ':scope' ? node : node?.querySelector(markSelector);
      const svg = mark?.querySelector('svg');
      const foreground = mark && getComputedStyle(mark).color;
      const background = node && getComputedStyle(node).backgroundColor;
      const labelNode = node?.querySelector('.new-session-option-label');
      const labelForeground = labelNode && getComputedStyle(labelNode).color;
      return node && {
        label: node.getAttribute('aria-label') || node.getAttribute('title'),
        className: node.className,
        svg: !!svg,
        glyphSignature: svg?.querySelector('g')?.innerHTML,
        markText: mark?.textContent.trim(),
        color: foreground,
        background,
        backgroundVariable: node && getComputedStyle(node).getPropertyValue('--bg').trim(),
        contrast: foreground && background ? ratio(foreground, background) : null,
        visibleLabel: labelNode?.textContent.trim(),
        labelColor: labelForeground,
        labelContrast: labelForeground && background ? ratio(labelForeground, background) : null,
      };
    };
    const machines = [...document.querySelectorAll('.new-session-machine')].map(node => ({
      host: node.dataset.loc, tagName: node.tagName, role: node.getAttribute('role'), ...identity(node, '.new-session-option-mark'),
    }));
    const pick = stream => {
      const row = document.querySelector(`.session-item[data-stream-id="${stream}"]`);
      return row && {
        ...identity(row.querySelector('.s-machine-avatar'), ':scope'),
        rowLabel: row.getAttribute('aria-label'),
        visibleName: row.querySelector('.s-name')?.textContent.trim(),
      };
    };
    return {
      appliedTheme: document.documentElement.dataset.theme,
      rootBackgroundVariable: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
      machines,
      assistant: pick(assistantStreamId),
      ordinary: pick(ordinaryStreamId),
      filters: [...document.querySelectorAll('.source-filter-btn[data-host]')]
        .filter(node => node.dataset.host !== 'all')
        .map(node => ({ host: node.dataset.host, ...identity(node, ':scope') })),
      stats: [...document.querySelectorAll('.machine-stat-mark')].map(node => identity(node, ':scope')),
      glyphs: Object.fromEntries(['ibis', 'sun', 'mage', 'djinni'].map(kind => [kind,
        window.PentacleCosmic.machineSigil(kind, { size: 25, color: 'currentColor' }).querySelector('g')?.innerHTML])),
    };
  }, { assistantStreamId, ordinaryStreamId });
}

async function main() {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PENTACLE_CHROME || undefined,
    args: ['--no-sandbox'],
  });
  const receipt = {
    urlSha256: sha(url), mode: overlay ? 'candidate-overlay' : 'served-runtime',
    source: require('node:child_process').execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
    browser: browser.version(),
    configSha256: sha(fs.readFileSync(configPath)),
    harnessSha256: sha(fs.readFileSync(__filename)),
    candidateAssets: Object.fromEntries(identityAssets
      .map(name => [name, sha(fs.readFileSync(path.join(dist, name)))])),
    overlaidAssets: {}, servedAssets: {}, cells: [],
  };
  try {
    for (const theme of ['dark', 'light']) for (const width of [1600, 850]) {
      const page = await browser.newPage({ viewport: { width, height: 900 }, ignoreHTTPSErrors: true });
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      await page.route('**/*', async route => {
          const requested = new URL(route.request().url());
          if (requested.origin === new URL(url).origin && identityAssets.includes(requested.pathname.slice(1))) {
            const file = path.join(dist, requested.pathname.slice(1));
            const original = overlay ? fs.readFileSync(file) : await (await route.fetch()).body();
            const originalSha = sha(original);
            const observed = overlay ? receipt.overlaidAssets : receipt.servedAssets;
            if (observed[requested.pathname]) assert.equal(observed[requested.pathname], originalSha, 'stable identity asset');
            observed[requested.pathname] = originalSha;
            if (!overlay) assert.equal(originalSha, expectedServed[requested.pathname.slice(1)], `deployed ${requested.pathname} matches release`);
            let body = original.toString('utf8');
            if (requested.pathname === '/bundle.js') {
              const seam = 'newSessionTerminalMode = !!terminalMode;';
              assert(body.includes(seam), 'test hook seam for New Terminal');
              body = body.replace(seam, `${seam}\nwindow.__pentacleIdentityTestState = () => ({ host: newSessionLocation, step: newSessionStep, terminal: newSessionTerminalMode });\nwindow.__pentacleTestOpenTerminal = () => showNewSessionModal(true);\nwindow.__pentacleTestSetTheme = theme => { state.appearance = { ...state.appearance, theme }; applyAppearanceSettings(); };\nwindow.__pentacleTestAttach = async (streamId, slot = 0) => { const row = document.querySelector('.session-item[data-stream-id="' + streamId + '"]'); if (!row) throw Error('missing session row ' + streamId); await attachSession(slot, row.dataset.name, row.dataset.display, row.dataset.host); };\nwindow.__pentacleTestDetach = slot => detachSlot(slot);\nwindow.__pentacleTestEmpty = slot => { const original = window.PentacleChatStore.selectSessionDetail; const priorQuestions = state.durableQuestionNotificationsById; state.durableQuestionNotificationsById = {}; window.PentacleChatStore.selectSessionDetail = (...args) => { const detail = original.apply(window.PentacleChatStore, args); return detail ? { ...detail, transcriptItems: [], remainingCount: 0 } : detail; }; try { renderSlotChat(slot); return { empty: document.querySelector('#cell-' + slot + ' .slot-chat-empty')?.textContent, heroCount: document.querySelectorAll('#cell-' + slot + ' .slot-chat-session-hero').length, rowCount: document.querySelectorAll('#cell-' + slot + ' .slot-chat-row').length, listText: document.querySelector('#cell-' + slot + ' .slot-chat-list')?.textContent.slice(0, 120), label: document.querySelector('#header-' + slot + ' .cell-label')?.textContent, lamp: !!document.querySelector('#header-' + slot + ' .cell-assistant-icon'), lampLabel: document.querySelector('#header-' + slot + ' .cell-assistant-icon')?.getAttribute('aria-label'), lampClass: document.querySelector('#header-' + slot + ' .cell-assistant-icon')?.className || '', lampColor: document.querySelector('#header-' + slot + ' .cell-assistant-icon') ? getComputedStyle(document.querySelector('#header-' + slot + ' .cell-assistant-icon')).color : undefined, host: document.querySelector('#header-' + slot + ' .cell-source-tag')?.textContent }; } finally { window.PentacleChatStore.selectSessionDetail = original; state.durableQuestionNotificationsById = priorQuestions; renderSlotChat(slot); } };`);
            }
            return route.fulfill({ body, contentType: file.endsWith('.css') ? 'text/css' : 'application/javascript' });
          }
          return route.continue();
        });
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      if (!overlay) {
        const build = await page.evaluate(async () => ({ config: window.__PENTACLE_CONFIG__?.buildId, live: await window.cc?.getBuild?.() }));
        assert.equal(build.config, config.expectedBuildId, 'served config build ID');
        assert.equal(build.live?.buildId || build.live, config.expectedBuildId, 'running host build ID');
        receipt.runningBuildId = config.expectedBuildId;
      }
      if (overlay) {
        await page.addStyleTag({ path: path.join(dist, 'chat_v3.css') });
        receipt.overlaidAssets['/chat_v3.css'] = receipt.candidateAssets['chat_v3.css'];
      }
      await page.waitForFunction(({ assistantStreamId, ordinaryStreamId }) => document.querySelectorAll('.source-filter-btn[data-host]').length >= 4
        && !!document.querySelector(`.session-item[data-stream-id="${assistantStreamId}"]`)
        && !!document.querySelector(`.session-item[data-stream-id="${ordinaryStreamId}"]`),
      { assistantStreamId, ordinaryStreamId }, { timeout: 15000 });
      await page.locator('#btn-new').click();
      await page.evaluate(value => window.__pentacleTestSetTheme(value), theme);
      await page.locator('.new-session-machine').first().waitFor();
      await page.evaluate(() => Promise.all([...document.querySelectorAll('.new-session-machine')]
        .flatMap(node => node.getAnimations().map(animation => animation.finished.catch(() => null)))));
      const cell = { theme, width, newChat: await inspect(page), navigation: [], errors };
      assert.equal(cell.newChat.appliedTheme, theme, 'actual application theme');
      for (const [host, spec] of Object.entries(expected)) {
        const machine = cell.newChat.machines.find(item => item.host === host);
        assertMachineIdentity(machine, spec, cell.newChat.glyphs, 'New Chat');
        assert.equal(machine.tagName, 'BUTTON', `New Chat ${host} native button`);
        const card = page.getByRole('button', { name: `Select ${spec.label} machine`, exact: true });
        assert.equal(await card.count(), 1, `New Chat ${host} accessible button`);
        if (host === Object.keys(expected)[0]) { await card.focus(); await page.keyboard.press('Enter'); }
        else await card.click();
        const step = await page.evaluate(() => ({
          host: window.__pentacleIdentityTestState()?.host,
          state: window.__pentacleIdentityTestState()?.step,
          profile: !!document.querySelector('.spawn-profile-controls'),
          machineVisible: !!document.querySelector('.new-session-machine'),
          title: document.querySelector('#new-session-title')?.textContent,
        }));
        cell.navigation.push({ kind: 'chat', host, step });
        assert.equal(step.host, host);
        assert.equal(step.machineVisible, false);
        assert.equal(step.state, 'profile');
        await page.locator('#new-session-cancel').click();
        await page.locator('#btn-new').click();
      }
      await page.locator('#new-session-cancel').click();
      await page.evaluate(() => window.__pentacleTestOpenTerminal());
      for (const [host, spec] of Object.entries(expected)) {
        const terminal = (await inspect(page)).machines.find(item => item.host === host);
        assertMachineIdentity(terminal, spec, cell.newChat.glyphs, 'New Terminal');
        assert.equal(terminal.tagName, 'BUTTON', `New Terminal ${host} native button`);
        const card = page.getByRole('button', { name: `Select ${spec.label} machine`, exact: true });
        assert.equal(await card.count(), 1, `New Terminal ${host} accessible button`);
        if (host === Object.keys(expected)[0]) { await card.focus(); await page.keyboard.press(' '); }
        else await card.click();
        const step = await page.evaluate(() => ({
          host: window.__pentacleIdentityTestState()?.host,
          state: window.__pentacleIdentityTestState()?.step,
          agents: [...document.querySelectorAll('.new-session-agent')].map(node => ({
            label: node.querySelector('.new-session-option-label')?.textContent,
            initial: node.querySelector('.new-session-option-mark')?.textContent.trim(),
            hostContext: node.querySelector('.new-session-option-meta')?.textContent,
          })),
        }));
        cell.navigation.push({ kind: 'terminal', host, card: terminal, step });
        assert.equal(step.host, host);
        assert.equal(step.state, 'agent');
        assert(step.agents.length > 0, `New Terminal ${host} agent step`);
        assert(step.agents.every(agent => agent.initial === agent.label?.[0]?.toUpperCase()
          && agent.hostContext === spec.label), `New Terminal ${host} provider initials remain labelled`);
        await page.locator('#new-session-cancel').click();
        await page.evaluate(() => window.__pentacleTestOpenTerminal());
      }
      await page.locator('#new-session-cancel').click();
      cell.sourceFilters = [];
      for (const [host, spec] of Object.entries(expected)) {
        const filter = cell.newChat.filters.find(item => item.label === spec.label);
        assert(filter?.svg && filter.label.includes(spec.label) && filter.className.includes(spec.color), `source filter ${host}: ${JSON.stringify(cell.newChat.filters)}`);
        assert.equal(filter.glyphSignature, cell.newChat.glyphs[spec.glyph], `source filter ${host} canonical glyph`);
        await page.locator(`.source-filter-btn[data-host="${filter.host}"]`).click();
        const selection = await page.evaluate(() => ({
          selected: document.querySelector('.source-filter-btn.active')?.getAttribute('aria-label'),
          visible: [...document.querySelectorAll('.session-item[data-stream-id]')]
            .filter(row => getComputedStyle(row).display !== 'none')
            .map(row => ({ stream: row.dataset.streamId, host: row.querySelector('.s-machine-avatar')?.getAttribute('aria-label') })),
        }));
        cell.sourceFilters.push({ host, selection });
        assert.equal(selection.selected, spec.label, `source filter ${host} selected`);
        assert(selection.visible.every(row => row.host === spec.label), `source filter ${host} visible sessions`);
        assert(cell.newChat.stats.some(item => item?.label?.includes(spec.label) && item.svg), `stats ${host}`);
        assert(cell.newChat.stats.some(item => item?.label?.includes(spec.label)
          && item.glyphSignature === cell.newChat.glyphs[spec.glyph]
          && item.className.includes(spec.color) && item.color === filter.color), `stats ${host} host-keyed glyph and colour`);
      }
      await page.locator('.source-filter-btn[data-host="all"]').click();
      assert(cell.newChat.assistant?.svg && cell.newChat.assistant.className.includes('color-forest-green'), 'assistant lamp');
      assert.equal(cell.newChat.assistant.glyphSignature, cell.newChat.glyphs.djinni, 'assistant canonical lamp');
      assert.equal(cell.newChat.assistant.label, assistant.iconLabel);
      assert(cell.newChat.assistant.rowLabel.includes(assistant.displayName));
      assert.equal(cell.newChat.assistant.visibleName, assistant.displayName);
      assert(cell.newChat.ordinary?.svg && cell.newChat.ordinary.className.includes(ordinary.color), 'ordinary host glyph');
      assert.equal(cell.newChat.ordinary.glyphSignature, cell.newChat.glyphs[ordinary.glyph], 'ordinary canonical host glyph');
      assert(cell.newChat.ordinary.rowLabel.includes(ordinary.displayName)
        && cell.newChat.ordinary.rowLabel.includes(ordinary.hostLabel));
      assert.equal(cell.newChat.ordinary.visibleName, ordinary.displayName);
      const statsByHost = items => Object.fromEntries(items.map(item => [item.label,
        { glyph: item.glyphSignature, className: item.className, color: item.color }]));
      cell.slotTransitions = [];
      for (const streamId of [assistantStreamId, ordinaryStreamId, null, assistantStreamId]) {
        if (streamId) await page.evaluate(id => window.__pentacleTestAttach(id, 0), streamId);
        else await page.evaluate(() => window.__pentacleTestDetach(0));
        if (streamId) {
          const chatToggle = page.locator('#header-0').getByRole('button', { name: 'Chat view', exact: true });
          if (await chatToggle.count()) await chatToggle.click();
          await page.waitForFunction(() => document.querySelector('#cell-0 .slot-chat-row')
            || /could not be loaded|No messages yet/i.test(document.querySelector('#cell-0 .slot-chat-empty')?.textContent || ''), null, { timeout: 12000 });
        }
        const transition = await page.evaluate(() => {
          const header = document.querySelector('#header-0');
          const lamp = header?.querySelector('.cell-assistant-icon');
          const greenProbe = document.createElement('span');
          greenProbe.style.color = 'var(--host-sigil-green)';
          document.body.appendChild(greenProbe);
          const expectedGreen = getComputedStyle(greenProbe).color;
          greenProbe.remove();
          return {
            label: header?.querySelector('.cell-label')?.textContent,
            lampLabel: lamp?.getAttribute('aria-label'),
            lampSignature: lamp?.querySelector('svg g')?.innerHTML,
            lampClass: lamp?.className || '',
            lampColor: lamp ? getComputedStyle(lamp).color : undefined,
            expectedGreen,
            host: header?.querySelector('.cell-source-tag')?.textContent,
            provider: header?.querySelector('.cell-provider-tag')?.textContent,
            heroCount: document.querySelectorAll('#cell-0 .slot-chat-session-hero').length,
            identityOrnaments: document.querySelectorAll('#cell-0 .slot-chat-session-hero, #cell-0 .slot-chat-session-icon, #cell-0 .slot-chat-v3-hero').length,
            transcriptRows: document.querySelectorAll('#cell-0 .slot-chat-row').length,
            emptyText: document.querySelector('#cell-0 .slot-chat-empty')?.textContent,
          };
        });
        cell.slotTransitions.push({ streamId, ...transition });
        assertSlotIdentity(transition, streamId, cell.newChat.glyphs);
        const currentStats = (await inspect(page)).stats;
        assert.deepEqual(statsByHost(currentStats), statsByHost(cell.newChat.stats),
          `stats stay host keyed after ${streamId || 'detach'}`);
        if (streamId && cell.slotTransitions.length <= 2) {
          const emptyFixture = await page.evaluate(() => window.__pentacleTestEmpty(0));
          transition.emptyFixture = emptyFixture;
          assert(emptyFixture.empty && /No messages yet|Syncing|Loading/i.test(emptyFixture.empty), `empty ${streamId} transcript state: ${JSON.stringify(emptyFixture)}`);
          assert.equal(emptyFixture.heroCount, 0, `empty ${streamId} has no hero`);
          assert.equal(emptyFixture.lamp, streamId === assistantStreamId, `empty ${streamId} slot identity`);
          assert.equal(emptyFixture.label, transition.label, `empty ${streamId} label persists`);
          assert.equal(emptyFixture.host, transition.host, `empty ${streamId} actual host persists`);
          if (streamId === assistantStreamId) {
            assert.equal(emptyFixture.lampLabel, assistant.iconLabel, 'empty assistant lamp accessible name');
            assert(emptyFixture.lampClass.includes('color-forest-green'), 'empty assistant green lamp class');
            assert.equal(emptyFixture.lampColor, transition.expectedGreen, 'empty assistant green lamp computed colour');
          }
        }
      }
      cell.statsAfterTransitions = (await inspect(page)).stats;
      assert.deepEqual(statsByHost(cell.statsAfterTransitions), statsByHost(cell.newChat.stats),
        'stats label, glyph, colour token and computed colour remain host keyed');
      cell.menuSweep = await page.evaluate(() => ({
        legacyMarks: [...document.querySelectorAll('.new-session-machine .new-session-option-mark, .s-machine-avatar, .source-filter-btn[data-host]:not([data-host="all"]), .machine-stat-mark')]
          .filter(node => /^[TAM]$/.test(node.textContent.trim())).map(node => node.outerHTML.slice(0, 250)),
        providerInitials: [...document.querySelectorAll('.new-session-agent .new-session-option-mark')].map(node => node.textContent.trim()),
        headerHostTags: [...document.querySelectorAll('.cell-source-tag')].map(node => node.textContent.trim()),
      }));
      assert.deepEqual(cell.menuSweep.legacyMarks, [], 'no legacy machine initials in visible identity marks');
      await page.locator('#settings-btn').click();
      cell.settingsSweep = await page.evaluate(() => ({
        open: getComputedStyle(document.querySelector('#settings-overlay')).display !== 'none',
        decorativeHostMarks: [...document.querySelectorAll('#settings-overlay .new-session-option-mark, #settings-overlay .machine-sigil')].length,
        labels: [...document.querySelectorAll('#settings-overlay label, #settings-overlay .setting-label')].map(node => node.textContent.trim()).filter(Boolean),
      }));
      assert(cell.settingsSweep.open && cell.settingsSweep.decorativeHostMarks === 0, 'settings menu has no legacy host identity marks');
      await page.locator('#settings-close').click();
      if (theme === 'dark' && width === 1600) {
        const firstHost = Object.keys(expected)[0];
        const machine = cell.newChat.machines.find(item => item.host === firstHost);
        const ordinaryTransition = cell.slotTransitions.find(item => item.streamId === ordinaryStreamId);
        const assistantTransition = cell.slotTransitions[0];
        const rejected = (label, fn) => { assert.throws(fn, label); return label; };
        cell.negativeControls = [
          rejected('reintroduced hero', () => assertSlotIdentity({ ...assistantTransition, heroCount: 1 }, assistantStreamId, cell.newChat.glyphs)),
          rejected('wrong host glyph', () => assertMachineIdentity({ ...machine, glyphSignature: cell.newChat.glyphs.sun }, expected[firstHost], cell.newChat.glyphs, 'New Chat')),
          rejected('wrong visible label', () => assertMachineIdentity({ ...machine, visibleLabel: 'X' }, expected[firstHost], cell.newChat.glyphs, 'New Chat')),
          rejected('lost button semantics', () => assertMachineIdentity({ ...machine, tagName: 'DIV' }, expected[firstHost], cell.newChat.glyphs, 'New Chat')),
          rejected('low contrast', () => assertMachineIdentity({ ...machine, labelContrast: 1.5 }, expected[firstHost], cell.newChat.glyphs, 'New Chat')),
          rejected('stale lamp after same-slot reuse', () => assertSlotIdentity({ ...ordinaryTransition, lampLabel: assistant.iconLabel }, ordinaryStreamId, cell.newChat.glyphs)),
          rejected('missing assistant host tag', () => assertSlotIdentity({ ...assistantTransition, host: undefined }, assistantStreamId, cell.newChat.glyphs)),
          rejected('wrong assistant lamp color', () => assertSlotIdentity({ ...assistantTransition, lampColor: 'rgb(0, 0, 0)' }, assistantStreamId, cell.newChat.glyphs)),
          rejected('stats color drift', () => assert.deepEqual({ ...statsByHost(cell.statsAfterTransitions), [expected[firstHost].label]: { ...statsByHost(cell.statsAfterTransitions)[expected[firstHost].label], color: 'rgb(0, 0, 0)' } }, statsByHost(cell.newChat.stats))),
          rejected('missing served asset', () => assertObservedAssets({ ...Object.fromEntries(identityAssets.map(name => [`/${name}`, receipt.candidateAssets[name]])), '/styles.css': undefined }, receipt.candidateAssets)),
          rejected('mismatched served asset', () => assertObservedAssets({ ...Object.fromEntries(identityAssets.map(name => [`/${name}`, receipt.candidateAssets[name]])), '/styles.css': 'wrong' }, receipt.candidateAssets)),
        ];
      }
      assert.deepEqual(errors, [], 'page errors');
      await page.screenshot({ path: path.join(output, `${theme}-${width}.png`) });
      receipt.cells.push(cell);
      await page.close();
    }
    receipt.status = 'PASS';
    if (!overlay) assertObservedAssets(receipt.servedAssets, expectedServed);
  } catch (error) {
    receipt.status = 'FAIL';
    receipt.error = String(error.stack || error);
    throw error;
  } finally {
    fs.writeFileSync(path.join(output, 'verdict.json'), JSON.stringify(receipt, null, 2) + '\n');
    await browser.close();
  }
  console.log(JSON.stringify({ status: receipt.status, cells: receipt.cells.length, output }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
