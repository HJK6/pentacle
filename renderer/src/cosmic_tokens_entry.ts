// Standalone browser entry for the public cosmic token bag.

import Tokens, {
  palette,
  Fonts,
  FONT_FAMILIES,
  MACHINES,
  MACHINE_ORDER,
  STATUS,
  SEV,
  TypeScale,
  Spacing,
  SCREEN_PAD,
  TOP_INSET,
} from './cosmic_tokens';

const PentacleCosmicTokens = {
  Tokens,
  palette,
  Fonts,
  FONT_FAMILIES,
  MACHINES,
  MACHINE_ORDER,
  STATUS,
  SEV,
  TypeScale,
  Spacing,
  SCREEN_PAD,
  TOP_INSET,
} as const;

declare global {
  interface Window {
    PentacleCosmicTokens: typeof PentacleCosmicTokens;
  }
}

if (typeof window !== 'undefined') window.PentacleCosmicTokens = PentacleCosmicTokens;

export {};
