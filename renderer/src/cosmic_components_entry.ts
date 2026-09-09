// public component layer (public cosmic component contract): standalone esbuild entry for the
// cosmic component library + tokens.
//
// Mirrors the public chat entry / public token entry pattern: re-exposes the
// vanilla-DOM/inline-SVG component factories AND the typed token bag on a single
// browser global so app.js (a classic <script>, not bundled) can call
// `window.PentacleCosmic.machineSigil(...)`, `arcaneRingFrame(...)`,
// `providerTag(...)`, `statusTag(...)`, etc. when it builds the chat surface.
//
// This is a SEPARATE esbuild entry/outfile (renderer/dist/cosmic_components.bundle.js
// via the `build:cosmic-components` npm script) and does NOT import or affect the
// chat bundle (renderer/dist/chat_core.bundle.js). The bundled
// `shared_transcript_view.ts` can instead `import` from './cosmic_components'
// directly; this global is for the non-bundled app.js call sites.

import {
  COSMIC_TOKENS,
  KIND_ACCENT,
  MACHINES,
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
} from './cosmic_components';

const PentacleCosmic = {
  COSMIC_TOKENS,
  KIND_ACCENT,
  MACHINES,
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
} as const;

declare global {
  interface Window {
    PentacleCosmic: typeof PentacleCosmic;
  }
}

if (typeof window !== 'undefined') {
  window.PentacleCosmic = PentacleCosmic;
}

export {};
