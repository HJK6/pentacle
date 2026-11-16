import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { MACHINES, MACHINE_ORDER, palette } from '../renderer/src/cosmic_tokens';
import { machineSigil } from '../renderer/src/cosmic_components';

const host = require('../renderer/host_presentation');

test('five machine accents, sigils and UI green', () => {
  assert.deepEqual(MACHINE_ORDER, ['djinni', 'sun', 'mage', 'flower', 'ibis']);
  assert.deepEqual(Object.fromEntries(MACHINE_ORDER.map(kind => [kind, MACHINES[kind].accent])), {
    djinni: '#1fbf4a', sun: '#ff2e3e', mage: '#1f5bff', flower: '#a377a1', ibis: '#ffd60a',
  });
  assert.equal(palette.green, '#3dff66');
  assert.equal(host.hostSigil({ chatStream: { hosts: ['thoth'] }, hostColors: { thoth: 'yellow' } }, 'thoth'), 'ibis');
  assert.equal(host.ACCENTS.yellow, '#ffd60a');
});

test('ibis SVG has the crescent, eye, beak and legs', () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  globalThis.document = dom.window.document;
  const svg = machineSigil('ibis');
  const paths = [...svg.querySelectorAll('path')].map(path => path.getAttribute('d'));
  assert.ok(paths.some(path => path?.startsWith('M52 5 A7.5')));
  assert.ok(paths.some(path => path?.startsWith('M19.5 12.5 C12 16')));
  assert.ok(paths.some(path => path?.startsWith('M38 45 L37 52')));
  assert.equal(svg.querySelector('circle[cx="24.2"]')?.getAttribute('fill'), '#ffd60a');
});
