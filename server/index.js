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
const crypto = require('node:crypto');
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
  '.webmanifest': 'application/manifest+json; charset=utf-8',
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
  const args = { port: DEFAULT_PORT, bind: DEFAULT_BIND, profile: null, tokenFile: null, tokenPath: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--profile') args.profile = argv[++i];
    else if (a === '--port') args.port = Number(argv[++i]);
    else if (a === '--bind') args.bind = argv[++i];
    else if (a === '--token-file') args.tokenFile = argv[++i];   // web login auth (this host)
    else if (a === '--token-path') args.tokenPath = argv[++i];   // chat-stream daemon credential
    else if (a === '--help' || a === '-h') args.help = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!Number.isInteger(args.port) || args.port < 0 || args.port > 65535) {
    throw new Error(`invalid --port: ${args.port}`);
  }
  // Normalize the bind so the address classified for auth is exactly the address
  // node listens on; an empty/whitespace bind (a wildcard listener) is rejected.
  args.bind = String(args.bind ?? '').trim();
  if (!args.bind) throw new Error('invalid --bind: empty (a bind address is required; default is 127.0.0.1)');
  return args;
}

// ── Auth ─────────────────────────────────────────────────────────────────────
// A loopback bind is single-user and needs no token, exactly as lane 1 shipped.
// Any routable bind MUST present a token file, or the host refuses to start, so
// a tailnet-hosted instance can never come up wide open. The browser posts the
// token once at /login; the reply sets an HttpOnly, SameSite=Strict cookie
// carrying sha256(token) (never the token itself), and every page, api call and
// websocket upgrade is gated on that cookie. No `Secure` attribute: the tailnet
// is the transport boundary and HTTPS is a named follow-up (see server/README).

function isLoopbackBind(bind) {
  // Fail CLOSED: an empty/whitespace/unspecified bind makes node listen on the
  // wildcard (all interfaces), so it must NOT be treated as loopback — otherwise
  // a `--bind ''` would come up on every interface with auth disabled.
  const b = String(bind ?? '').trim().toLowerCase();
  if (!b) return false;
  return b === 'localhost' || b === '::1' || b === '::ffff:127.0.0.1' || /^127(\.\d{1,3}){3}$/.test(b);
}

function readTokenFile(tokenFile) {
  let raw;
  try {
    raw = fs.readFileSync(tokenFile, 'utf8');
  } catch (e) {
    throw new Error(`cannot read --token-file ${tokenFile}: ${e.code || e.message}`);
  }
  const token = raw.trim();
  if (!token) throw new Error(`--token-file ${tokenFile} is empty`);
  return token;
}

function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ba.length !== bb.length) return false;
  return crypto.timingSafeEqual(ba, bb);
}

function parseCookies(header) {
  const out = {};
  if (!header) return out;
  for (const part of String(header).split(';')) {
    const eq = part.indexOf('=');
    if (eq < 0) continue;
    const name = part.slice(0, eq).trim();
    if (name) out[name] = part.slice(eq + 1).trim();
  }
  return out;
}

const COOKIE_NAME = 'pentacle_web';

function createAuth(token) {
  const cookieValue = crypto.createHash('sha256').update(token).digest('hex');
  return {
    isAuthed(req) {
      const cookie = parseCookies(req.headers && req.headers.cookie)[COOKIE_NAME];
      return !!cookie && safeEqual(cookie, cookieValue);
    },
    checkToken(candidate) { return safeEqual(candidate, token); },
    setCookieHeader() {
      return `${COOKIE_NAME}=${cookieValue}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800`;
    },
  };
}

function loginPage(error = '') {
  const banner = error ? `<p class="err">${error}</p>` : '';
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pentacle — sign in</title>
<style>body{font:15px/1.4 system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#181b22;padding:28px;border-radius:12px;box-shadow:0 6px 30px rgba(0,0,0,.4);width:min(340px,90vw)}
h1{font-size:18px;margin:0 0 14px}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;background:#0f1115;color:#e6e6e6;font-size:15px}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font-size:15px;cursor:pointer}
.err{color:#f87171;margin:0 0 12px}</style></head>
<body><form method="POST" action="/login"><h1>Pentacle web host</h1>${banner}
<input type="password" name="token" placeholder="Access token" autofocus autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>`;
}

function handleLogin(req, res, auth) {
  if (req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' }).end(loginPage());
    return;
  }
  if (req.method !== 'POST') { res.writeHead(405).end('method not allowed'); return; }
  let body = '';
  req.on('data', (chunk) => {
    body += chunk;
    if (body.length > 4096) { req.destroy(); }  // a token is short; cap the body
  });
  req.on('end', () => {
    const token = new URLSearchParams(body).get('token') || '';
    if (auth.checkToken(token)) {
      res.writeHead(302, { 'set-cookie': auth.setCookieHeader(), location: '/', 'cache-control': 'no-store' }).end();
    } else {
      res.writeHead(401, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' }).end(loginPage('Incorrect token.'));
    }
  });
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
    console.log('usage: node server --profile <name> [--port 7795] [--bind 127.0.0.1] [--token-file <path>] [--token-path <path>]');
    return 0;
  }

  // Bind guard, before anything is allocated: a routable bind without a token
  // is a refusal, not a warning. Loopback stays open (single-user); a token on
  // loopback opts that instance into auth anyway.
  const requireAuth = !isLoopbackBind(args.bind);
  let auth = null;
  if (requireAuth || args.tokenFile) {
    if (requireAuth && !args.tokenFile) {
      throw new Error(`refusing to bind routable address ${args.bind} without --token-file (a loopback bind needs no token)`);
    }
    auth = createAuth(readTokenFile(args.tokenFile));
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
  // The chat-stream v2 operator auth refuses an *implicit* credential path
  // (error `operator_auth_v2_private_path_required`), so a web host talking to a
  // credentialed daemon must name the credential explicitly: `--token-path`, or
  // `chatStream.tokenPath` in the profile. It stays server-side — `publicConfig`
  // strips `token`/`tokenPath`, so it never reaches the browser.
  if (args.tokenPath) {
    CONFIG.chatStream = { ...(CONFIG.chatStream || {}), tokenPath: args.tokenPath };
  }

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
    // When auth is on, /login is the only unauthenticated surface; a GET
    // navigation for anything else is redirected there and every api call gets
    // a 401. Loopback (auth === null) is served exactly as before.
    if (auth) {
      if (urlPath === '/login') { handleLogin(req, res, auth); return; }
      if (!auth.isAuthed(req)) {
        if (req.method === 'GET' && !urlPath.startsWith('/api/')) {
          res.writeHead(302, { location: '/login', 'cache-control': 'no-store' }).end();
        } else {
          res.writeHead(401, { 'content-type': 'text/plain; charset=utf-8' }).end('authentication required');
        }
        return;
      }
    }
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

  const wss = new WebSocketServer({
    server,
    path: '/cc',
    // The upgrade carries the browser's cookies; reject an unauthenticated one
    // before it becomes a socket. Loopback (auth === null) accepts every upgrade.
    verifyClient: auth
      ? (info, done) => (auth.isAuthed(info.req) ? done(true) : done(false, 401, 'authentication required'))
      : undefined,
  });
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
  console.log(`[web] auth ${auth ? 'ENABLED (token cookie required)' : 'disabled (loopback, single-user)'}`);

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

module.exports = { main, parseArgs, resolveProfilePath, serveStatic, WEB_DIST, isLoopbackBind, createAuth, COOKIE_NAME };

if (require.main === module) {
  main().catch((e) => {
    console.error(`[web] failed to start: ${e && e.stack ? e.stack : e}`);
    process.exit(1);
  });
}
