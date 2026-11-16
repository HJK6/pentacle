'use strict';

// Computed-style + CSS contract for the chat-mode question launcher + modal.
//
// Two defects came in with the web chat v3 floating launcher:
//   1. The floating .slot-chat-question container kept the base inline-dock box
//      (border + fill + radius), so a rectangle framed the pulsing circle.
//   2. Inside the modal the same node kept position:absolute from chat_v3.css
//      (the portal override never reset it), so the question content escaped the
//      centered frame and stretched across / past the viewport.
//
// Coverage split:
//   - The floating-launcher box removal is asserted from the *computed* cascade
//     in jsdom (the winning rule for the floating context also loads last, so
//     jsdom's source-order cascade matches a real browser here).
//   - The modal-dock position:static fix depends on selector *specificity*
//     (.desktop-question-portal.cosmic … 0,3,0 beating .cosmic … 0,2,0 that
//     loads later). jsdom resolves the cascade by source order, not specificity,
//     so that fix is asserted as a CSS source contract here and verified as a
//     real computed style + real layout in a browser. Both fail on the pre-fix CSS.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
// Same order as renderer/index.html (chat_v3.css loads last).
const CSS_FILES = ['styles.css', 'cosmic_theme.css', 'cosmic_chat_surface.css', 'chat_v3.css'];

function read(f) {
  return fs.readFileSync(path.join(root, 'renderer', f), 'utf8');
}

function buildDom() {
  const styles = CSS_FILES
    .map((f) => `<style data-src="${f}">${read(f)}</style>`)
    .join('\n');
  return new JSDOM(`<!doctype html><html><head>${styles}</head><body>
    <div class="cosmic">
      <div class="slot-chat-shell">
        <div class="slot-chat-question" id="floating">
          <button type="button" class="slot-chat-question-open" id="circle"></button>
        </div>
      </div>
    </div>
  </body></html>`);
}

function zeroWidth(v) {
  return v === '0px' || v === '0' || v === '';
}
function transparentBg(v) {
  return v === 'rgba(0, 0, 0, 0)' || v === 'transparent' || v === '' || v === 'none';
}

test('floating question launcher shows only the pulsing circle — no box (computed style)', () => {
  const dom = buildDom();
  const w = dom.window;
  const icon = w.getComputedStyle(w.document.getElementById('floating'));

  // Keeps its floating placement bottom-right...
  assert.equal(icon.position, 'absolute', 'launcher stays absolutely positioned');
  // ...but no box around the circle: no border, no fill, no radius.
  for (const side of ['borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth']) {
    assert.ok(zeroWidth(icon[side]), `launcher ${side} must be 0 (was ${icon[side]})`);
  }
  assert.ok(transparentBg(icon.backgroundColor), `launcher background must be transparent (was ${icon.backgroundColor})`);
  assert.ok(zeroWidth(icon.borderTopLeftRadius), `launcher border-radius must be 0 (was ${icon.borderTopLeftRadius})`);
  dom.window.close();
});

test('modal question dock is reset to normal flow (CSS contract; specificity-dependent)', () => {
  const css = read('cosmic_chat_surface.css');
  // The portal-scoped reset block that neutralises the shared node inside the
  // modal must also reset position and re-enable pointer events. Pre-fix this
  // block had no `position`, so position:absolute leaked in from chat_v3.css.
  const block = css.match(/\.desktop-question-portal\.cosmic\s+\.slot-chat-question\s*\{[^}]*position:\s*static[^}]*\}/);
  assert.ok(block, 'portal dock reset must declare position: static');
  assert.match(block[0], /pointer-events:\s*auto/, 'portal dock reset must declare pointer-events: auto');
});

test('floating-launcher box reset is declared in chat_v3.css (CSS contract)', () => {
  const css = read('chat_v3.css');
  const block = css.match(/\.cosmic\s+\.slot-chat-question\s*\{[^}]*\}/);
  assert.ok(block, 'floating .cosmic .slot-chat-question rule present');
  assert.match(block[0], /position:\s*absolute/, 'launcher keeps position: absolute');
  assert.match(block[0], /border:\s*0/, 'launcher resets border');
  assert.match(block[0], /background:\s*none/, 'launcher resets background');
  assert.match(block[0], /border-radius:\s*0/, 'launcher resets border-radius');
});
