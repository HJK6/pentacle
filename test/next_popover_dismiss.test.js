'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { JSDOM } = require('jsdom');

test('question actions that redraw the popover do not dismiss it as outside clicks', () => {
  const source = fs.readFileSync(path.join(__dirname, '../renderer/next/entry.js'), 'utf8');
  const listener = source.split('\n').find(line => line.startsWith("document.addEventListener('click', event =>"));
  assert.ok(listener, 'the entry point registers the document click-away handler');
  const dom = new JSDOM('<div class="rail"></div><div class="popover"></div><button id="outside">Outside</button>');
  const { document, MouseEvent } = dom.window;
  const popover = document.querySelector('.popover');
  const rail = document.querySelector('.rail');
  const state = { popover: 'questions' };
  let closes = 0;
  const closePopover = () => { closes += 1; state.popover = null; };
  new Function('document', 'state', 'popover', 'rail', 'closePopover', listener)(document, state, popover, rail, closePopover);

  for (const action of ['send', 'retry']) {
    const button = document.createElement('button');
    button.textContent = action;
    popover.append(button);
    button.addEventListener('click', () => popover.replaceChildren());
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true }));
    assert.equal(closes, 0, `${action} redraw stays inside the popover interaction`);
    assert.equal(state.popover, 'questions');
  }
  document.querySelector('#outside').dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true }));
  assert.equal(closes, 1, 'a genuine outside click still dismisses');
  dom.window.close();
});
