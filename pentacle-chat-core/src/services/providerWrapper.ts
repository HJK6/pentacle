import type { PentacleEvent } from '../types/pentacle';

// Mirrors provider_wrappers.py. Shared vectors live in provider-wrapper.json.
const PROVIDER_WRAPPERS = [{
  provider: 'claude',
  kind: 'claude_pasted_content' as const,
  pattern: /^\n\n<pasted_content id="([0-9]+)">\n([\s\S]*)\n<\/pasted_content id="\1">\n$/,
}];

export function normalizeProviderUserText(text: string, provider: string, authenticated = false): {
  text: string;
  provider_wrapper?: PentacleEvent['provider_wrapper'];
} {
  if (!authenticated) return { text };
  for (const wrapper of PROVIDER_WRAPPERS) {
    if (provider !== wrapper.provider) continue;
    const match = wrapper.pattern.exec(text);
    // JS $ may stop before a final LF; require the same fullmatch as Python.
    if (match && match[0] === text) {
      return { text: match[2], provider_wrapper: {
        kind: wrapper.kind, id: match[1], provenance: 'grammar',
      } };
    }
  }
  return { text };
}

/** Wire events arrive through the authenticated daemon channel. */
export function providerDisplayText(event: PentacleEvent): string {
  // Tagged text is already display text. Do not strip a literal inner wrapper.
  if (event.provider_wrapper) return event.text;
  const authenticated = event.kind === 'USER'
    && event.raw?.source === 'structured' && event.raw?.transport === 'claude-jsonl';
  return normalizeProviderUserText(event.text, event.provider, authenticated).text;
}

// Python re \s includes U+001C..001F and U+0085, but excludes U+FEFF (JS differs).
const PYTHON_WHITESPACE = /[\t\n\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g;
const ANSI = /\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g;

/** Exactly the daemon's normalize_submission_text fallback for string inputs. */
export function normalizeSubmissionText(text: string): string {
  return text.replace(ANSI, '').replace(/\r/g, '\n').replace(/\u00a0/g, ' ')
    .replace(PYTHON_WHITESPACE, ' ').replace(/^ +| +$/g, '');
}
