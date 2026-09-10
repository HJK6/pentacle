// Workstream C (public_chat_ui): cosmic component library tests.
//
// jsdom unit tests for `renderer/src/cosmic_components.ts`. Each factory is
// asserted to render the expected SVG/DOM structure, and the ported `<Path d>`
// attributes are SNAPSHOTTED against the mobile source
// (`public-mobile/src/components/`) so a future drift is caught.
//
// Run via esbuild bundle -> `node --test` (see scripts/run-tests.js),
// mirroring the existing shared-core view test harness. jsdom is a devDependency.

import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

// Module load is document-free (no top-level DOM access), so wiring the jsdom
// globals here — before any test() callback runs a factory — is sufficient.
const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>');
(globalThis as unknown as { document: Document }).document = dom.window.document;
(globalThis as unknown as { window: unknown }).window = dom.window;

import {
  machineSigil,
  arcaneRingFrame,
  starfield,
  bevel,
  bevelPath,
  spark,
  brackets,
  spinner,
  bar,
  pill,
  providerTag,
  statusTag,
  sevTag,
  COSMIC_TOKENS,
  KIND_ACCENT,
  MACHINES,
} from '../renderer/src/cosmic_components';

function paths(el: Element): string[] {
  return Array.from(el.querySelectorAll('path')).map((p) => p.getAttribute('d') || '');
}

// jsdom normalizes inline-style hex colors to `rgb(r, g, b)`. SVG presentation
// *attributes* (stroke/fill) are NOT normalized, so we only need this for the
// HTML factories' `.style.*` reads.
function rgb(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

// ---------------------------------------------------------------------------
// Verbatim path `d` snapshots copied from the mobile .tsx sources. A reviewer
// (and this test) diffs these against MachineSigil.tsx etc.
// ---------------------------------------------------------------------------

const DJINNI_PATHS = [
  'M4 26 C9 29 13 30 18 30.5 C28 31 40 31 46 30.5 C50.5 31.5 51.5 35.5 48 38.5 C44 42.5 36 44.5 30 44.5 C22 44.5 14 41.5 11 37.5 C8 33.5 6 30 4 26 Z',
  'M25.5 30.5 C25.5 23 38.5 23 38.5 30.5',
  'M29.5 23.4 C29.5 21.4 34.5 21.4 34.5 23.4',
  'M47 31 C57 29 59.5 39.5 51 41.5 C48.3 42.1 47.7 39.8 49.6 38.8',
  'M30 44.5 L29.2 49 M34 44.5 L34.8 49',
  'M26 51.5 C27 49 37 49 38 51.5',
];

const MAGE_PATHS = [
  'M26 6 C24.5 12 22 18 19 23 L33 23 C30 18 27.5 12 26 6 Z',
  'M15 23.5 C20 27 32 27 37 23.5',
  'M22.6 30.4 C22 38 24 43 26 45 C28 43 30 38 29.4 30.4',
  'M20 32 C17 43 15.4 50 14.5 55.4 L37.5 55.4 C36.6 49 34.6 40 32 32',
  'M26 45 L26 55.2',
  'M17 46.5 C24 49.4 31 49.4 35.4 46.5',
  'M32 39 C37 37.6 41 38 44 39.4',
  'M45.6 13 L43.6 56',
  'M46 4.4 L46 7.4 M51.6 10.4 L48.8 10.4 M50 6.4 L48.1 8.2',
];

const SUN_BODY_PATHS = [
  'M32 16 C36 23 41 27 41 35 A9 9 0 1 1 23 35 C23 29 26 26 28 22 C29.5 27 31 28.5 32 30 C34.5 25 32 20 32 16 Z',
  'M32 31 C34 33 34.5 36 33 38 A3.2 3.2 0 1 1 29.6 35.5 C29.6 34 30.8 33 32 31 Z',
];

const FLOWER_PATHS = [
  'M27 22 Q29 20 31 22 M33 22 Q35 20 37 22',
  'M30 23.5 L29.5 26 M32 23.5 L32 26.5 M34 23.5 L34.5 26',
  'M32 30 L32 58',
  'M32 48 C40 45 45 51 44 58',
  'M32 40 C25 38 21 43 22 49',
];

const SPARK_PATH = 'M12 1.5 L13.8 9.3 L21.5 11.1 L13.8 12.9 L12 20.7 L10.2 12.9 L2.5 11.1 L10.2 9.3 Z';

// Independent re-derivation of MachineSigil.tsx's MageStar helper.
function expectedMageStar(cx: number, cy: number, r: number): string {
  const pts: string[] = [];
  for (let i = 0; i < 8; i += 1) {
    const a = (i * Math.PI) / 4;
    const rr = i % 2 ? r * 0.4 : r;
    pts.push(`${cx + rr * Math.sin(a)},${cy - rr * Math.cos(a)}`);
  }
  return `M${pts.join(' L')} Z`;
}

// ---------------------------------------------------------------------------
// machineSigil
// ---------------------------------------------------------------------------

test('machineSigil returns a 64x64 <svg> with round-cap 2.4 stroke group', () => {
  const el = machineSigil('djinni');
  assert.equal(el.tagName.toLowerCase(), 'svg');
  assert.equal(el.getAttribute('viewBox'), '0 0 64 64');
  const g = el.querySelector('g');
  assert.ok(g, 'has a <g> wrapper');
  assert.equal(g!.getAttribute('stroke-width'), '2.4');
  assert.equal(g!.getAttribute('stroke-linecap'), 'round');
  assert.equal(g!.getAttribute('stroke-linejoin'), 'round');
  assert.equal(g!.getAttribute('fill'), 'none');
});

test('machineSigil defaults color to the kind accent', () => {
  const g = machineSigil('mage').querySelector('g')!;
  assert.equal(g.getAttribute('stroke'), KIND_ACCENT.mage);
  assert.equal(KIND_ACCENT.mage, '#29d4ff');
  // explicit override wins
  const g2 = machineSigil('mage', { color: '#abcdef' }).querySelector('g')!;
  assert.equal(g2.getAttribute('stroke'), '#abcdef');
});

test('machineSigil(djinni) ports every path verbatim + accent dot', () => {
  const el = machineSigil('djinni');
  assert.deepEqual(paths(el), DJINNI_PATHS);
  const circle = el.querySelector('circle')!;
  assert.equal(circle.getAttribute('cx'), '32');
  assert.equal(circle.getAttribute('cy'), '18.4');
  assert.equal(circle.getAttribute('fill'), KIND_ACCENT.djinni);
  assert.equal(circle.getAttribute('stroke'), 'none');
});

test('machineSigil(mage) ports paths + the MageStar + two circles', () => {
  const el = machineSigil('mage');
  const got = paths(el);
  // The MageStar path is computed; assert it matches an independent derivation.
  const star = expectedMageStar(25, 15, 2.6);
  assert.ok(got.includes(star), 'mage star path present');
  // and every literal mage path is present, in order, around the star.
  for (const d of MAGE_PATHS) assert.ok(got.includes(d), `mage path present: ${d}`);
  // star is filled with the accent, stroke none
  const starEl = Array.from(el.querySelectorAll('path')).find((p) => p.getAttribute('d') === star)!;
  assert.equal(starEl.getAttribute('fill'), KIND_ACCENT.mage);
  assert.equal(starEl.getAttribute('stroke'), 'none');
  // two circles: the head (stroke only) + the staff orb (filled accent)
  const circles = Array.from(el.querySelectorAll('circle'));
  assert.equal(circles.length, 2);
  assert.equal(circles[0].getAttribute('r'), '3.3');
  assert.equal(circles[1].getAttribute('fill'), KIND_ACCENT.mage);
});

test('machineSigil(sun) emits 12 rays + two body paths with tinted fills', () => {
  const el = machineSigil('sun');
  const lines = el.querySelectorAll('line');
  assert.equal(lines.length, 12, '12 rays (0..330 step 30)');
  assert.deepEqual(paths(el), SUN_BODY_PATHS);
  const [halo, core] = Array.from(el.querySelectorAll('path'));
  assert.equal(halo.getAttribute('fill'), `${KIND_ACCENT.sun}22`);
  assert.equal(core.getAttribute('fill'), KIND_ACCENT.sun);
  assert.equal(core.getAttribute('stroke'), 'none');
  // first ray geometry (a=0): r1=20 -> (52,32), long -> r2=30 -> (62,32)
  assert.equal(lines[0].getAttribute('x1'), '52');
  assert.equal(lines[0].getAttribute('y1'), '32');
  assert.equal(lines[0].getAttribute('x2'), '62');
});

test('machineSigil(flower) emits 6 petals + center + face/stem paths', () => {
  const el = machineSigil('flower');
  const ellipses = el.querySelectorAll('ellipse');
  assert.equal(ellipses.length, 6, '6 petals (0..300 step 60)');
  assert.equal(ellipses[0].getAttribute('transform'), 'rotate(0 32 24)');
  assert.equal(ellipses[1].getAttribute('transform'), 'rotate(60 32 24)');
  assert.deepEqual(paths(el), FLOWER_PATHS);
  const center = el.querySelector('circle')!;
  assert.equal(center.getAttribute('r'), '6');
  assert.equal(center.getAttribute('fill'), `${KIND_ACCENT.flower}22`);
});

// ---------------------------------------------------------------------------
// arcaneRingFrame
// ---------------------------------------------------------------------------

test('arcaneRingFrame draws outer ring + dashed inner + 12 ticks and hosts the sigil', () => {
  const frame = arcaneRingFrame({ machine: 'mage', size: 80 });
  assert.equal(frame.tagName.toLowerCase(), 'div');
  const ring = frame.querySelector('svg[viewBox="0 0 100 100"]')!;
  assert.ok(ring, 'ring svg present');
  // The ring svg contains only its own 2 circles + 12 ticks; the sigil is a
  // sibling in the frame, so an unscoped query stays within the ring.
  const circles = Array.from(ring.querySelectorAll('circle'));
  assert.equal(circles.length, 2);
  assert.equal(circles[0].getAttribute('r'), '47');
  assert.equal(circles[0].getAttribute('stroke-width'), '1.4');
  assert.equal(circles[1].getAttribute('r'), '34');
  assert.equal(circles[1].getAttribute('stroke-dasharray'), '1.5 3');
  assert.equal(ring.querySelectorAll('line').length, 12, '12 ticks');
  // accent resolved from the machine, and the mage sigil is embedded
  assert.equal(circles[0].getAttribute('stroke'), MACHINES['mage'].accent);
  const sigil = frame.querySelector('svg[viewBox="0 0 64 64"]');
  assert.ok(sigil, 'embeds the machine sigil svg');
});

test('arcaneRingFrame honors explicit children over the sigil', () => {
  const marker = document.createElement('span');
  marker.id = 'child-marker';
  const frame = arcaneRingFrame({ size: 64, children: marker });
  assert.ok(frame.querySelector('#child-marker'));
  assert.equal(frame.querySelector('svg[viewBox="0 0 64 64"]'), null);
});

// ---------------------------------------------------------------------------
// starfield
// ---------------------------------------------------------------------------

test('starfield renders a deterministic 44-dot field (same seed/positions as mobile)', () => {
  const field = starfield();
  const dots = field.querySelectorAll('.cosmic-star');
  assert.equal(dots.length, 44);
  // Re-derive the first star from the mobile LCG (seed 7).
  let s = 7;
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280;
    return s / 233280;
  };
  const x0 = rnd() * 100;
  const y0 = rnd() * 100;
  const first = dots[0] as HTMLElement;
  assert.equal(first.style.left, `${x0}%`);
  assert.equal(first.style.top, `${y0}%`);
  assert.equal(first.style.backgroundColor, rgb(COSMIC_TOKENS.palette.star));
});

// ---------------------------------------------------------------------------
// bevel
// ---------------------------------------------------------------------------

test('bevel cuts top-right + bottom-left corners (verbatim path formula)', () => {
  const el = bevel({ width: 100, height: 40, cut: 12 });
  assert.equal(el.tagName.toLowerCase(), 'svg');
  const d = el.querySelector('path')!.getAttribute('d');
  // bevel = min(12, 50, 20) = 12 -> M0 0 H88 L100 12 V40 H12 L0 28 Z
  assert.equal(d, 'M0 0 H88 L100 12 V40 H12 L0 28 Z');
  assert.equal(bevelPath(100, 40, 12), 'M0 0 H88 L100 12 V40 H12 L0 28 Z');
  assert.equal(el.querySelector('path')!.getAttribute('fill'), COSMIC_TOKENS.palette.panel);
});

test('bevelPath clamps cut to half the smaller dimension and empties on zero size', () => {
  // cut clamped to height/2 = 5
  assert.equal(bevelPath(100, 10, 12), 'M0 0 H95 L100 5 V10 H5 L0 5 Z');
  assert.equal(bevelPath(0, 40, 12), '');
});

// ---------------------------------------------------------------------------
// atoms
// ---------------------------------------------------------------------------

test('spark is a 24x24 star path filled with the accent', () => {
  const el = spark({ color: '#3dff66' });
  assert.equal(el.getAttribute('viewBox'), '0 0 24 24');
  assert.equal(el.querySelector('path')!.getAttribute('d'), SPARK_PATH);
  assert.equal(el.querySelector('path')!.getAttribute('fill'), '#3dff66');
});

test('brackets renders the </> chevrons', () => {
  const el = brackets();
  assert.deepEqual(paths(el), ['M9 7l-5 5 5 5', 'M15 7l5 5-5 5']);
  assert.equal(el.querySelector('path')!.getAttribute('stroke-width'), '2.2');
});

test('spinner renders track + dashed arc and injects rotation keyframes once', () => {
  const el = spinner({ size: 30, strokeWidth: 3 });
  const circles = el.querySelectorAll('circle');
  assert.equal(circles.length, 2);
  assert.equal(circles[0].getAttribute('stroke-opacity'), '0.18');
  const radius = (30 - 3) / 2;
  const c = 2 * Math.PI * radius;
  assert.equal(circles[1].getAttribute('stroke-dasharray'), `${c * 0.75} ${c * 0.25}`);
  // keyframes injected exactly once
  spinner();
  assert.equal(document.querySelectorAll('#cosmic-spinner-keyframes').length, 1);
});

test('bar clamps the fill width to 0..100', () => {
  assert.equal((bar({ pct: 150, color: '#fff' }).firstElementChild as HTMLElement).style.width, '100%');
  assert.equal((bar({ pct: -5, color: '#fff' }).firstElementChild as HTMLElement).style.width, '0%');
  assert.equal((bar({ pct: 42, color: '#fff' }).firstElementChild as HTMLElement).style.width, '42%');
});

test('pill renders a tinted bordered label', () => {
  const el = pill('SUMMON', { color: '#29d4ff' });
  assert.equal(el.textContent, 'SUMMON');
  assert.equal(el.style.borderColor, rgb('#29d4ff'));
});

// ---------------------------------------------------------------------------
// tags
// ---------------------------------------------------------------------------

test('providerTag(claude) shows the spark + "Claude"', () => {
  const el = providerTag('claude');
  assert.equal(el.querySelector('path')!.getAttribute('d'), SPARK_PATH);
  assert.match(el.textContent || '', /Claude/);
});

test('providerTag(codex) shows the </> brackets + "Codex"', () => {
  const el = providerTag('PROVIDER_C');
  assert.deepEqual(paths(el), ['M9 7l-5 5 5 5', 'M15 7l5 5-5 5']);
  assert.match(el.textContent || '', /Codex/);
});

test('statusTag(working) shows the spinner ring; idle shows a plain circle', () => {
  const working = statusTag('working');
  assert.ok(working.querySelector('.activity-spinner'), 'shared spinner present while working');
  assert.equal(working.querySelector('.cosmic-spinner'), null);
  assert.equal((working.textContent || '').trim(), '');

  const idle = statusTag('idle');
  assert.equal(idle.querySelector('.cosmic-spinner'), null);
  const circle = idle.querySelector('circle')!;
  assert.equal(circle.getAttribute('r'), '5');
  assert.equal(circle.getAttribute('stroke'), COSMIC_TOKENS.palette.muted);
  assert.match(idle.textContent || '', /Idle/);
});

test('statusTag(unresponsive) renders the exact client label', () => {
  const unresponsive = statusTag('unresponsive');
  assert.equal(unresponsive.querySelector('.activity-spinner'), null);
  assert.match(unresponsive.textContent || '', /Unresponsive/);
});

test('sevTag(critical) uses the critical hex and an uppercased label', () => {
  const el = sevTag('critical');
  assert.equal(el.textContent, 'CRITICAL');
  assert.equal(COSMIC_TOKENS.severity.critical, '#ff2e3e');
  assert.equal(el.style.borderColor, rgb('#ff2e3e'));
  // background is the critical hex at ~6% alpha (`${color}10`) -> rgba base match
  assert.match(el.style.backgroundColor, /^rgba\(255, 46, 62,/);
});

test('sevTag maps info/warning to their hexes', () => {
  assert.equal(sevTag('info').style.borderColor, rgb('#3dff66'));
  assert.equal(sevTag('warning').style.borderColor, rgb('#ffb53d'));
});
