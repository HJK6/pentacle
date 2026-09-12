'use strict';

// esbuild entry for the browser bundle. window.cc must exist before app.js runs
// (app.js reads it at module scope), and app.js is the only script that needs
// bundling — the rest of renderer/index.html's scripts are UMD/classic files
// that publish window globals and stay <script src> tags (see
// scripts/build-web.js EXTERNAL_SCRIPTS).
//
// test/web_bundle.test.js pins this against index.html, so a script added to
// the desktop page cannot silently go missing from the web page.

require('./web_cc').installWebCc();
require('./app.js');
