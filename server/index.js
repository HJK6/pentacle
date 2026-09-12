#!/usr/bin/env node
'use strict';

// ── Pentacle headless web host ───────────────────────────────────────────────
// Serves the esbuild-bundled renderer over HTTP and exposes the same
// `window.cc` surface over a websocket, owning the tmux attachments and the
// single chat-stream connection that the Electron main process owns on the
// desktop.
//
//   node server --profile <name> [--port 7795] [--bind 127.0.0.1]
//
// Loopback only by default. Auth, multi-user isolation and a tailnet bind are
// deliberately out of scope; see server/README.md.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { WebSocketServer } = require('ws');

const ROOT = path.join(__dirname, '..');
const { loadConfig } = require(path.join(ROOT, 'config-loader'));
const { createCcHandlers, createCollector } = require(path.join(ROOT, 'main', 'cc_handlers'));
const { createWsBridge } = require('./ws_bridge');
const chatStreamClient = require(path.join(ROOT, 'main', 'chat_stream_client'));

const DEFAULT_PORT = 7795;
const DEFAULT_BIND = '127.0.0.1';
const WEB_DIST = path.join(ROOT, 'renderer', 'dist', 'web');
// Per-browser-connection terminal ceiling. The renderer uses four slots; the
// headroom covers reconnect churn while still bounding what one connection can
// allocate on a shared (e.g. tailnet-hosted) instance.
const MAX_PTYS_PER_CONNECTION = 8;

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.wasm': 'application/wasm',
};

function parseArgs(argv) {
  const args = { port: DEFAULT_PORT, bind: DEFAULT_BIND, profile: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--profile') args.profile = argv[++i];
    else if (a === '--port') args.port = Number(argv[++i]);
    else if (a === '--bind') args.bind = argv[++i];
    else if (a === '--help' || a === '-h') args.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!Number.isInteger(args.port) || args.port < 0 || args.port > 65535) {
    throw new Error(`invalid --port: ${args.port}`);
  }
  return args;
}

// Mirrors config-loader's candidate order for a named profile: a machine-local
// override first, then the tracked config. A path (or a `*.js` argument) is
// used verbatim so a test config outside configs/ can be passed straight
// through.
function resolveProfilePath(profile) {
  if (!profile) return null;
  if (profile.includes(path.sep) || profile.includes('/') || profile.endsWith('.js')) {
    return path.resolve(profile);
  }
  const local = path.join(ROOT, 'configs', `${profile}.local.js`);
  if (fs.existsSync(local)) return local;
  return path.join(ROOT, 'configs', `${profile}.js`);
}

function serveStatic(res, urlPath, configJson) {
  const rel = urlPath === '/' ? 'web.html' : urlPath.replace(/^\/+/, '');
  const target = path.resolve(WEB_DIST, rel);
  // Path traversal guard — a request must not escape the bundle directory.
  if (target !== WEB_DIST && !target.startsWith(WEB_DIST + path.sep)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  let body;
  try {
    body = fs.readFileSync(target);
  } catch {
    if (rel === 'web.html') {
      res.writeHead(503, { 'content-type': 'text/plain; charset=utf-8' })
        .end('The web bundle is not built yet. Run `npm run build:web`.');
      return;
    }
    res.writeHead(404).end('not found');
    return;
  }
  const ext = path.extname(target).toLowerCase();
  if (ext === '.html') {
    // Inject the computed config before any bundle script runs, so the
    // config-loader shim can answer synchronously exactly like the preload does.
    body = Buffer.from(String(body).replace(
      '<!--PENTACLE_CONFIG-->',
      `<script>window.__PENTACLE_CONFIG__=${JSON.stringify(configJson).replace(/</g, '\\u003c')};</script>`,
    ));
  }
  res.writeHead(200, {
    'content-type': CONTENT_TYPES[ext] || 'application/octet-stream',
    'cache-control': 'no-cache',
  }).end(body);
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.help) {
    console.log('usage: node server --profile <name> [--port 7795] [--bind 127.0.0.1]');
    return 0;
  }

  const profilePath = resolveProfilePath(args.profile);
  const env = profilePath ? { ...process.env, PENTACLE_CONFIG: profilePath } : process.env;
  let CONFIG;
  let configError = null;
  let configWarnings = [];
  let configPath = null;
  try {
    const loaded = loadConfig(ROOT, env);
    CONFIG = loaded.config;
    configPath = loaded.path;
    configWarnings = loaded.warnings || [];
  } catch (error) {
    configError = error;
    CONFIG = { appName: 'Pentacle', features: { mic: false }, hosts: { local: { kind: 'local' } }, agents: {} };
  }

  // The same handler set main.js registers on ipcMain, collected into a table.
  const ccHandlers = createCcHandlers({ CONFIG, chatStreamClient, configError, configWarnings,
    terminalOptions: { maxPtysPerConnection: MAX_PTYS_PER_CONNECTION } });
  const collector = createCollector();
  const stopTerminals = ccHandlers.register(collector);
  const bridge = createWsBridge({ table: collector.table });

  // One daemon connection per host process, exactly as the desktop does; every
  // browser tab shares it and receives the frames as `chat-stream:frame` pushes.
  if (CONFIG.chatStream?.url) {
    chatStreamClient.init(CONFIG, (frame) => bridge.broadcast('chat-stream:frame', frame));
  }

  const configJson = ccHandlers.publicConfig();
  const server = http.createServer((req, res) => {
    const urlPath = new URL(req.url, 'http://localhost').pathname;
    if (urlPath === '/api/config') {
      res.writeHead(200, { 'content-type': CONTENT_TYPES['.json'], 'cache-control': 'no-cache' })
        .end(JSON.stringify(ccHandlers.publicConfig()));
      return;
    }
    if (urlPath === '/api/health') {
      res.writeHead(200, { 'content-type': CONTENT_TYPES['.json'] })
        .end(JSON.stringify({ ok: true, connections: bridge.connections.size }));
      return;
    }
    serveStatic(res, urlPath, configJson);
  });

  const wss = new WebSocketServer({ server, path: '/cc' });
  wss.on('connection', (socket) => {
    bridge.addSocket(socket);
    socket.on('message', (raw) => { bridge.handleMessage(socket, raw.toString()); });
    socket.on('close', () => bridge.removeSocket(socket));
    socket.on('error', () => bridge.removeSocket(socket));
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(args.port, args.bind, resolve);
  });
  const actual = server.address();
  if (configError) console.warn(`[web] using fallback configuration: ${configError.message || configError}`);
  console.log(`[web] config ${configPath || '(fallback)'}`);
  console.log(`[web] serving ${WEB_DIST}`);
  console.log(`[web] listening on http://${args.bind}:${actual.port}  (ws ${actual.port}/cc)`);

  // One close for everything the host owns, so a caller (tests, a harness) can
  // shut it down without leaking timers or terminal attachments.
  const close = async () => {
    try { stopTerminals(); } catch {}
    try { chatStreamClient.destroy(); } catch {}
    bridge.closeAll();
    for (const socket of wss.clients) { try { socket.terminate(); } catch {} }
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => server.close(resolve));
  };

  const shutdown = () => {
    close().finally(() => process.exit(0));
    setTimeout(() => process.exit(0), 2000).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  return { server, wss, bridge, handlers: collector.table, close, port: actual.port, url: `http://${args.bind}:${actual.port}` };
}

module.exports = { main, parseArgs, resolveProfilePath, serveStatic, WEB_DIST };

if (require.main === module) {
  main().catch((e) => {
    console.error(`[web] failed to start: ${e && e.stack ? e.stack : e}`);
    process.exit(1);
  });
}
