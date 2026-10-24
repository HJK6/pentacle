'use strict';

// Common Chrome launch policy. Playwright supplies its own headless switch.
function headlessChromeArgs(extra = [], { playwright = false } = {}) {
  return [
    ...(playwright ? [] : ['--headless=new']),
    '--no-first-run', '--no-default-browser-check', '--use-mock-keychain',
    ...extra,
  ];
}
module.exports = { headlessChromeArgs };
