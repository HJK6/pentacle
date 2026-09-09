// Public component layer (public cosmic component contract): cosmic component library port.
//
// Vanilla DOM / inline-SVG factory functions reimplementing the public
// DOM cosmic component library so the Electron renderer can
// render the same "cosmic / arcane" sigils, frames, atoms and tags WITHOUT
// public DOM. Each factory returns a live DOM node (`SVGElement` /
// `HTMLElement`); string consumers (e.g. the innerHTML path in
// `shared_transcript_view.ts`) can use `node.outerHTML`.
//
// SVG fidelity is the bar: every `<Path d=...>` / `<Circle>` / `<Line>` /
// `<Ellipse>` and the MageStar / sun-ray / petal / star-seed helpers are ported
// derived from the public component fixtures under
// `public component fixtures/`:
//   MachineSigil.tsx, ArcaneRingFrame.tsx, Starfield.tsx, Bevel.tsx,
//   ArcaneAtoms.tsx, ProviderTag.tsx, StatusTag.tsx, SevTag.tsx
// Flat reskin — no shadows/glows.
//
// Tokens: public component layer owns `renderer/src/cosmic_tokens.ts` — the SINGLE source
// of truth, derived from the public token set. C
// originally duplicated the few values it needs as an inline `COSMIC_TOKENS`
// (with a TODO to reconcile once B landed). That inline copy is now REMOVED:
// `COSMIC_TOKENS` below is mapped directly onto B's typed exports so the palette
// / fonts / type-scale / spacing / status / severity values have exactly one
// definition. The export name + shape (palette/fonts/type/spacing/status/
// severity) are preserved so existing consumers and the component tests are
// unaffected.

import {
  palette,
  Fonts,
  TypeScale,
  Spacing,
  STATUS,
  SEV,
  MACHINES as TOKEN_MACHINES,
} from './cosmic_tokens';

// ---------------------------------------------------------------------------
// Tokens (single source of truth: renderer/src/cosmic_tokens.ts)
// ---------------------------------------------------------------------------

export const COSMIC_TOKENS = {
  palette,
  fonts: Fonts,
  type: TypeScale,
  spacing: Spacing,
  status: STATUS,
  severity: SEV,
} as const;

export type MachineSigilKind = 'djinni' | 'sun' | 'mage' | 'flower';
export type ProviderKind = 'claude' | 'codex';
export type WorkStatus = 'working' | 'idle';
export type Severity = 'info' | 'warning' | 'critical';

// MACHINES name->{kind,accent,epithet} — re-exported from the single source of
// truth (cosmic_tokens.ts) so the machine table is not duplicated here.
export const MACHINES = TOKEN_MACHINES;

// Per-kind accent — used as the default sigil color. public fixture sigil
// defaults `color` to palette.green and relies on callers (ArcaneRingFrame /
// screens) to pass the machine accent; per the public component contract ("color
// defaults to the machine accent") we default to the kind's accent instead.
// Derived from MACHINES so it stays in lockstep with the single source of truth.
// This does NOT affect any ported path `d` — only the stroke color.
export const KIND_ACCENT: Record<MachineSigilKind, string> = {
  djinni: MACHINES['hosta'].accent,
  sun: MACHINES['hostb'].accent,
  mage: MACHINES['hostc'].accent,
  flower: MACHINES['hostd'].accent,
};

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const SVG_NS = 'http://www.w3.org/2000/svg';

type Attrs = Record<string, string | number | undefined>;

function setAttrs(node: Element, attrs: Attrs): void {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined) continue;
    node.setAttribute(key, String(value));
  }
}

// Create an SVG-namespaced element with attributes + optional children.
function svg(tag: string, attrs: Attrs = {}, children: Element[] = []): SVGElement {
  const node = document.createElementNS(SVG_NS, tag) as SVGElement;
  setAttrs(node, attrs);
  for (const child of children) node.appendChild(child);
  return node;
}

// Create an HTML element with inline styles + optional attrs/children/text.
function html<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  opts: { style?: Partial<CSSStyleDeclaration>; attrs?: Attrs; text?: string; children?: Node[] } = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (opts.style) Object.assign(node.style, opts.style);
  if (opts.attrs) setAttrs(node, opts.attrs);
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.children) for (const child of opts.children) node.appendChild(child);
  return node;
}

// Shared stroke props for the 64x64 sigils (public: fill none, stroke color,
// strokeWidth 2.4, round caps/joins). Applied to the <g> wrapper.
function sigilStrokeAttrs(color: string): Attrs {
  return {
    fill: 'none',
    stroke: color,
    'stroke-width': 2.4,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
  };
}

function path(d: string, extra: Attrs = {}): SVGElement {
  return svg('path', { d, ...extra });
}

// ---------------------------------------------------------------------------
// MachineSigil.tsx -> machineSigil(kind, opts?)
// ---------------------------------------------------------------------------

export type MachineSigilOpts = { size?: number; color?: string };

// MageStar helper — ported verbatim from MachineSigil.tsx.
function mageStarPath(cx: number, cy: number, r: number, color: string): SVGElement {
  const pts: string[] = [];
  for (let i = 0; i < 8; i += 1) {
    const a = (i * Math.PI) / 4;
    const rr = i % 2 ? r * 0.4 : r;
    pts.push(`${cx + rr * Math.sin(a)},${cy - rr * Math.cos(a)}`);
  }
  return path(`M${pts.join(' L')} Z`, { fill: color, stroke: 'none' });
}

export function machineSigil(kind: MachineSigilKind, opts: MachineSigilOpts = {}): SVGElement {
  const size = opts.size ?? 64;
  const color = opts.color ?? KIND_ACCENT[kind];
  const root = svg('svg', { width: size, height: size, viewBox: '0 0 64 64' });
  const g = svg('g', sigilStrokeAttrs(color));
  root.appendChild(g);

  if (kind === 'djinni') {
    g.appendChild(path('M4 26 C9 29 13 30 18 30.5 C28 31 40 31 46 30.5 C50.5 31.5 51.5 35.5 48 38.5 C44 42.5 36 44.5 30 44.5 C22 44.5 14 41.5 11 37.5 C8 33.5 6 30 4 26 Z'));
    g.appendChild(path('M25.5 30.5 C25.5 23 38.5 23 38.5 30.5'));
    g.appendChild(path('M29.5 23.4 C29.5 21.4 34.5 21.4 34.5 23.4'));
    g.appendChild(svg('circle', { cx: 32, cy: 18.4, r: 2.6, fill: color, stroke: 'none' }));
    g.appendChild(path('M47 31 C57 29 59.5 39.5 51 41.5 C48.3 42.1 47.7 39.8 49.6 38.8'));
    g.appendChild(path('M30 44.5 L29.2 49 M34 44.5 L34.8 49'));
    g.appendChild(path('M26 51.5 C27 49 37 49 38 51.5'));
    return root;
  }

  if (kind === 'mage') {
    g.appendChild(path('M26 6 C24.5 12 22 18 19 23 L33 23 C30 18 27.5 12 26 6 Z'));
    g.appendChild(path('M15 23.5 C20 27 32 27 37 23.5'));
    g.appendChild(mageStarPath(25, 15, 2.6, color));
    g.appendChild(svg('circle', { cx: 26, cy: 27.6, r: 3.3 }));
    g.appendChild(path('M22.6 30.4 C22 38 24 43 26 45 C28 43 30 38 29.4 30.4'));
    g.appendChild(path('M20 32 C17 43 15.4 50 14.5 55.4 L37.5 55.4 C36.6 49 34.6 40 32 32'));
    g.appendChild(path('M26 45 L26 55.2'));
    g.appendChild(path('M17 46.5 C24 49.4 31 49.4 35.4 46.5'));
    g.appendChild(path('M32 39 C37 37.6 41 38 44 39.4'));
    g.appendChild(path('M45.6 13 L43.6 56'));
    g.appendChild(svg('circle', { cx: 46, cy: 10.4, r: 3, fill: color, stroke: 'none' }));
    g.appendChild(path('M46 4.4 L46 7.4 M51.6 10.4 L48.8 10.4 M50 6.4 L48.1 8.2'));
    return root;
  }

  if (kind === 'sun') {
    for (let a = 0; a < 360; a += 30) {
      const rad = (a * Math.PI) / 180;
      const long = (a / 30) % 2 === 0;
      const r1 = 20;
      const r2 = long ? 30 : 26;
      g.appendChild(
        svg('line', {
          x1: 32 + r1 * Math.cos(rad),
          y1: 32 + r1 * Math.sin(rad),
          x2: 32 + r2 * Math.cos(rad),
          y2: 32 + r2 * Math.sin(rad),
        }),
      );
    }
    g.appendChild(
      path('M32 16 C36 23 41 27 41 35 A9 9 0 1 1 23 35 C23 29 26 26 28 22 C29.5 27 31 28.5 32 30 C34.5 25 32 20 32 16 Z', {
        fill: `${color}22`,
      }),
    );
    g.appendChild(
      path('M32 31 C34 33 34.5 36 33 38 A3.2 3.2 0 1 1 29.6 35.5 C29.6 34 30.8 33 32 31 Z', {
        fill: color,
        stroke: 'none',
      }),
    );
    return root;
  }

  // flower (default)
  for (let a = 0; a < 360; a += 60) {
    g.appendChild(
      svg('ellipse', { cx: 32, cy: 14.5, rx: 4.2, ry: 8, transform: `rotate(${a} 32 24)` }),
    );
  }
  g.appendChild(svg('circle', { cx: 32, cy: 24, r: 6, fill: `${color}22` }));
  g.appendChild(path('M27 22 Q29 20 31 22 M33 22 Q35 20 37 22'));
  g.appendChild(path('M30 23.5 L29.5 26 M32 23.5 L32 26.5 M34 23.5 L34.5 26'));
  g.appendChild(path('M32 30 L32 58'));
  g.appendChild(path('M32 48 C40 45 45 51 44 58'));
  g.appendChild(path('M32 40 C25 38 21 43 22 49'));
  return root;
}

// ---------------------------------------------------------------------------
// ArcaneRingFrame.tsx -> arcaneRingFrame(opts)
// ---------------------------------------------------------------------------

export type ArcaneRingFrameOpts = {
  size?: number;
  color?: string;
  kind?: MachineSigilKind;
  machine?: keyof typeof MACHINES;
  sigilSize?: number;
  children?: Node;
};

export function arcaneRingFrame(opts: ArcaneRingFrameOpts = {}): HTMLElement {
  const size = opts.size ?? 64;
  const machineMeta = opts.machine ? MACHINES[opts.machine] : undefined;
  const accent = opts.color ?? machineMeta?.accent ?? COSMIC_TOKENS.palette.green;
  const sigilKind = opts.kind ?? machineMeta?.kind;

  const frame = html('div', {
    attrs: { class: 'cosmic-ring-frame' },
    style: {
      width: `${size}px`,
      height: `${size}px`,
      position: 'relative',
      flexShrink: '0',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    },
  });

  const ring = svg('svg', { width: size, height: size, viewBox: '0 0 100 100' });
  Object.assign((ring as unknown as SVGElement & { style: CSSStyleDeclaration }).style, {
    position: 'absolute',
    top: '0',
    left: '0',
  });
  ring.appendChild(svg('circle', { cx: 50, cy: 50, r: 47, fill: 'none', stroke: accent, 'stroke-width': 1.4, opacity: 0.6 }));
  ring.appendChild(
    svg('circle', {
      cx: 50,
      cy: 50,
      r: 34,
      fill: 'none',
      stroke: accent,
      'stroke-width': 0.8,
      'stroke-dasharray': '1.5 3',
      opacity: 0.45,
    }),
  );
  for (let a = 0; a < 360; a += 30) {
    const rad = (a * Math.PI) / 180;
    const r = (a / 30) % 3 === 0 ? 38 : 41;
    ring.appendChild(
      svg('line', {
        x1: 50 + r * Math.cos(rad),
        y1: 50 + r * Math.sin(rad),
        x2: 50 + 45 * Math.cos(rad),
        y2: 50 + 45 * Math.sin(rad),
        stroke: accent,
        'stroke-width': 1,
        opacity: 0.5,
      }),
    );
  }
  frame.appendChild(ring);

  if (opts.children) {
    frame.appendChild(opts.children);
  } else if (sigilKind) {
    frame.appendChild(machineSigil(sigilKind, { size: opts.sigilSize ?? size * 0.62, color: accent }));
  }

  return frame;
}

// ---------------------------------------------------------------------------
// Starfield.tsx -> starfield(opts)
// ---------------------------------------------------------------------------

export type StarfieldOpts = { n?: number; seed?: number };

type Star = { x: number; y: number; r: number; o: number };

// Deterministic LCG seed -> star list (ported verbatim from Starfield.tsx).
function makeStars(n = 44, seed = 7): Star[] {
  let s = seed;
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280;
    return s / 233280;
  };
  return Array.from({ length: n }, () => ({
    x: rnd() * 100,
    y: rnd() * 100,
    r: 0.7 + rnd() * 1.8,
    o: 0.14 + rnd() * 0.45,
  }));
}

const DEFAULT_STARS = makeStars();

export function starfield(opts: StarfieldOpts = {}): HTMLElement {
  const n = opts.n ?? 44;
  const seed = opts.seed ?? 7;
  const stars = n === 44 && seed === 7 ? DEFAULT_STARS : makeStars(n, seed);

  const root = html('div', {
    attrs: { class: 'cosmic-starfield' },
    style: {
      position: 'absolute',
      top: '0',
      left: '0',
      right: '0',
      bottom: '0',
      overflow: 'hidden',
      pointerEvents: 'none',
    },
  });

  for (const star of stars) {
    root.appendChild(
      html('div', {
        attrs: { class: 'cosmic-star' },
        style: {
          position: 'absolute',
          left: `${star.x}%`,
          top: `${star.y}%`,
          width: `${star.r}px`,
          height: `${star.r}px`,
          borderRadius: `${star.r / 2}px`,
          opacity: String(star.o),
          backgroundColor: COSMIC_TOKENS.palette.star,
        },
      }),
    );
  }
  return root;
}

// ---------------------------------------------------------------------------
// Bevel.tsx -> bevel(opts)
// ---------------------------------------------------------------------------

export type BevelOpts = {
  width: number;
  height: number;
  cut?: number;
  fill?: string;
  stroke?: string;
  strokeWidth?: number;
};

// Beveled path (cut top-right + bottom-left). Ported verbatim from Bevel.tsx:
//   `M0 0 H${width - bevel} L${width} ${bevel} V${height} H${bevel} L0 ${height - bevel} Z`
// Exported so consumers (public component layer) can recompute on resize (public uses
// onLayout); `bevel()` itself snapshots the path for the given dimensions.
export function bevelPath(width: number, height: number, cut: number = COSMIC_TOKENS.spacing.bevelCard): string {
  const w = Math.max(0, width);
  const h = Math.max(0, height);
  const b = Math.min(cut, w / 2, h / 2);
  if (!w || !h) return '';
  return `M0 0 H${w - b} L${w} ${b} V${h} H${b} L0 ${h - b} Z`;
}

export function bevel(opts: BevelOpts): SVGElement {
  const { width, height } = opts;
  const cut = opts.cut ?? COSMIC_TOKENS.spacing.bevelCard;
  const fill = opts.fill ?? COSMIC_TOKENS.palette.panel;
  const stroke = opts.stroke ?? COSMIC_TOKENS.palette.line;
  const strokeWidth = opts.strokeWidth ?? 1;

  const root = svg('svg', {
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'none',
    class: 'cosmic-bevel',
  });
  (root as unknown as { style: CSSStyleDeclaration }).style.pointerEvents = 'none';
  root.appendChild(path(bevelPath(width, height, cut), { fill, stroke, 'stroke-width': strokeWidth }));
  return root;
}

// ---------------------------------------------------------------------------
// ArcaneAtoms.tsx -> spark() / brackets() / spinner() / bar() (+ pill())
// ---------------------------------------------------------------------------

export type IconOpts = { size?: number; color?: string };

export function spark(opts: IconOpts = {}): SVGElement {
  const size = opts.size ?? 12;
  const color = opts.color ?? COSMIC_TOKENS.palette.green;
  const root = svg('svg', { width: size, height: size, viewBox: '0 0 24 24' });
  root.appendChild(path('M12 1.5 L13.8 9.3 L21.5 11.1 L13.8 12.9 L12 20.7 L10.2 12.9 L2.5 11.1 L10.2 9.3 Z', { fill: color }));
  return root;
}

export function brackets(opts: IconOpts = {}): SVGElement {
  const size = opts.size ?? 12;
  const color = opts.color ?? COSMIC_TOKENS.palette.green;
  const root = svg('svg', { width: size, height: size, viewBox: '0 0 24 24' });
  const common: Attrs = {
    stroke: color,
    'stroke-width': 2.2,
    fill: 'none',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
  };
  root.appendChild(path('M9 7l-5 5 5 5', common));
  root.appendChild(path('M15 7l5 5-5 5', common));
  return root;
}

// One-time @keyframes for the spinner rotation (public uses Animated.loop /
// 900ms linear). Self-contained so the atom works without external CSS.
function ensureSpinnerKeyframes(): void {
  if (typeof document === 'undefined') return;
  if (document.getElementById('cosmic-spinner-keyframes')) return;
  const style = document.createElement('style');
  style.id = 'cosmic-spinner-keyframes';
  style.textContent = '@keyframes cosmic-spin{to{transform:rotate(360deg)}}';
  (document.head || document.documentElement).appendChild(style);
}

export type SpinnerOpts = IconOpts & { strokeWidth?: number };

export function spinner(opts: SpinnerOpts = {}): HTMLElement {
  const size = opts.size ?? 30;
  const color = opts.color ?? COSMIC_TOKENS.palette.green;
  const strokeWidth = opts.strokeWidth ?? 3;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;

  ensureSpinnerKeyframes();

  const wrap = html('div', {
    attrs: { class: 'cosmic-spinner' },
    style: {
      width: `${size}px`,
      height: `${size}px`,
      animation: 'cosmic-spin 0.9s linear infinite',
    },
  });
  const root = svg('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}` });
  root.appendChild(
    svg('circle', {
      cx: size / 2,
      cy: size / 2,
      r: radius,
      stroke: color,
      'stroke-width': strokeWidth,
      'stroke-opacity': 0.18,
      fill: 'none',
    }),
  );
  root.appendChild(
    svg('circle', {
      cx: size / 2,
      cy: size / 2,
      r: radius,
      stroke: color,
      'stroke-width': strokeWidth,
      'stroke-dasharray': `${circumference * 0.75} ${circumference * 0.25}`,
      'stroke-linecap': 'round',
      fill: 'none',
    }),
  );
  wrap.appendChild(root);
  return wrap;
}

export type BarOpts = { pct: number; color: string };

export function bar(opts: BarOpts): HTMLElement {
  const width = Math.max(0, Math.min(100, opts.pct));
  const track = html('div', {
    attrs: { class: 'cosmic-bar-track' },
    style: {
      height: '6px',
      borderRadius: '999px',
      backgroundColor: COSMIC_TOKENS.palette.codePanel,
      overflow: 'hidden',
    },
  });
  track.appendChild(
    html('div', {
      attrs: { class: 'cosmic-bar-fill' },
      style: {
        width: `${width}%`,
        height: '100%',
        borderRadius: '999px',
        backgroundColor: opts.color,
      },
    }),
  );
  return track;
}

// Pill (ArcaneAtoms.Pill) — border + tinted bg, JetBrains Mono label.
export function pill(text: string, opts: { color?: string } = {}): HTMLElement {
  const color = opts.color ?? COSMIC_TOKENS.palette.green;
  return html('div', {
    attrs: { class: 'cosmic-pill' },
    style: {
      borderWidth: '1px',
      borderStyle: 'solid',
      borderColor: color,
      backgroundColor: `${color}12`,
      borderRadius: '999px',
      padding: '4px 8px',
      fontFamily: COSMIC_TOKENS.fonts.jetBrainsMono.medium,
      fontSize: `${COSMIC_TOKENS.type.label}px`,
      letterSpacing: '0.6px',
      color,
    },
    text,
  });
}

// ---------------------------------------------------------------------------
// ProviderTag.tsx -> providerTag(provider, opts?)
// ---------------------------------------------------------------------------

export type ProviderTagOpts = { color?: string };

export function providerTag(provider: ProviderKind | string, opts: ProviderTagOpts = {}): HTMLElement {
  const color = opts.color ?? COSMIC_TOKENS.palette.green;
  const isClaude = String(provider).toLowerCase() === 'claude';

  const root = html('div', {
    attrs: { class: 'cosmic-provider-tag' },
    style: { display: 'flex', flexDirection: 'row', alignItems: 'center', gap: '5px' },
  });
  root.appendChild(isClaude ? spark({ size: 12, color }) : brackets({ size: 12, color }));
  root.appendChild(
    html('span', {
      attrs: { class: 'cosmic-provider-tag-label' },
      style: {
        fontFamily: COSMIC_TOKENS.fonts.jetBrainsMono.medium,
        fontSize: `${COSMIC_TOKENS.type.meta}px`,
        letterSpacing: '0.5px',
        color,
      },
      text: isClaude ? 'Claude' : 'Codex',
    }),
  );
  return root;
}

// ---------------------------------------------------------------------------
// StatusTag.tsx -> statusTag(status, opts?)
// ---------------------------------------------------------------------------

export type StatusTagOpts = { color?: string };

export function statusTag(status: WorkStatus | string, opts: StatusTagOpts = {}): HTMLElement {
  const normalizedStatus = String(status).toLowerCase();
  const working = normalizedStatus === 'working';
  const label = normalizedStatus
    ? `${normalizedStatus.slice(0, 1).toUpperCase()}${normalizedStatus.slice(1)}`
    : 'Idle';
  const accent = opts.color ?? (working ? COSMIC_TOKENS.status.working : COSMIC_TOKENS.palette.muted);

  const root = html('div', {
    attrs: { class: 'cosmic-status-tag' },
    style: { display: 'flex', flexDirection: 'row', alignItems: 'center', gap: '5px' },
  });

  if (working) {
    root.appendChild(
      html('span', {
        attrs: { class: 'activity-spinner' },
        style: {
          borderColor: `${accent}4D`,
          borderTopColor: accent,
        },
      }),
    );
  } else {
    const idle = svg('svg', { width: 12, height: 12, viewBox: '0 0 12 12' });
    idle.appendChild(svg('circle', { cx: 6, cy: 6, r: 5, fill: 'none', stroke: accent, 'stroke-width': 1.6, opacity: 0.6 }));
    root.appendChild(idle);
  }

  if (!working) {
    root.appendChild(
      html('span', {
        attrs: { class: 'cosmic-status-tag-label' },
        style: {
          fontFamily: COSMIC_TOKENS.fonts.jetBrainsMono.medium,
          fontSize: `${COSMIC_TOKENS.type.label}px`,
          letterSpacing: '1px',
          color: accent,
        },
        text: label,
      }),
    );
  }
  return root;
}

// ---------------------------------------------------------------------------
// SevTag.tsx -> sevTag(severity)
// ---------------------------------------------------------------------------

function severityColor(severity: string): string {
  const key = severity.toLowerCase() as Severity;
  return COSMIC_TOKENS.severity[key] ?? COSMIC_TOKENS.palette.green;
}

export function sevTag(severity: Severity | string): HTMLElement {
  const label = String(severity || 'info').toUpperCase();
  const color = severityColor(label);

  return html('div', {
    attrs: { class: 'cosmic-sev-tag' },
    style: {
      alignSelf: 'flex-start',
      borderWidth: '1px',
      borderStyle: 'solid',
      borderColor: color,
      backgroundColor: `${color}10`,
      borderRadius: '999px',
      padding: '2px 7px',
      fontFamily: COSMIC_TOKENS.fonts.jetBrainsMono.bold,
      fontSize: `${COSMIC_TOKENS.type.label}px`,
      letterSpacing: '0.6px',
      color,
    },
    text: label,
  });
}
