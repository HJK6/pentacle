'use strict';

// ── window.cc over a websocket ───────────────────────────────────────────────
// Dispatches the shared handler table (main/cc_handlers.js) from browser
// requests and pushes main→renderer events back out.
//
// Wire protocol (JSON, one message per frame):
//   request   { id, method, args }            browser → host
//   response  { id, ok: true,  result }       host → browser
//             { id, ok: false, error: { message, code } }
//   push      { event, args }                 host → browser
//
// `send`-mode methods (pty:write, pty:resize, …) mirror ipcRenderer.send: they
// are fire-and-forget and get no response, exactly as in Electron.
//
// Each connection gets a stand-in for Electron's `event.sender`, which is what
// makes per-tab isolation fall out for free: main/terminal_adapter.js keys its
// slots by `event.sender.id` and pushes pty output through `event.sender.send`,
// so one tab's terminals and output can never reach another's. Closing a socket
// emits `destroyed`, which is the same signal the adapter already listens for
// to tear a window's attachments down.

const { WEB_LOCAL, WEB_UNSUPPORTED } = require('../main/cc_handlers');

function errorPayload(id, code, message) {
  return { id, ok: false, error: { code, message } };
}

function createWsBridge({ table, logger = console } = {}) {
  const connections = new Map();
  let nextSenderId = 1;

  function rawSend(socket, payload) {
    try {
      socket.send(JSON.stringify(payload));
    } catch (e) {
      logger.warn(`[web] send failed: ${e.message}`);
    }
  }

  /** Electron's `event.sender`, as much of it as the handlers actually use. */
  function createSender(socket) {
    let destroyed = false;
    const listeners = new Map();
    return {
      id: `web-${nextSenderId++}`,
      send(channel, ...args) { if (!destroyed) rawSend(socket, { event: channel, args }); },
      isDestroyed() { return destroyed; },
      once(name, fn) { listeners.set(name, fn); },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        const fn = listeners.get('destroyed');
        listeners.clear();
        if (fn) {
          try { fn(); } catch (e) { logger.warn(`[web] destroyed handler threw: ${e.message}`); }
        }
      },
    };
  }

  const bridge = {
    connections,

    addSocket(socket) {
      const sender = createSender(socket);
      connections.set(socket, { sender, event: { sender } });
      return sender;
    },

    removeSocket(socket) {
      const connection = connections.get(socket);
      connections.delete(socket);
      // Tears down this tab's terminal attachments through the adapter's own
      // lifecycle hook, so a reload cannot leak them.
      connection?.sender.destroy();
    },

    /** Handle one raw websocket frame from `socket`. */
    async handleMessage(socket, raw) {
      const connection = connections.get(socket);
      if (!connection) return undefined;

      let message;
      try {
        message = JSON.parse(typeof raw === 'string' ? raw : String(raw));
      } catch {
        return rawSend(socket, errorPayload(null, 'bad_request', 'message is not JSON'));
      }
      if (!message || typeof message !== 'object' || typeof message.method !== 'string') {
        return rawSend(socket, errorPayload(message && message.id, 'bad_request', 'missing method'));
      }

      const { id, method } = message;
      const args = Array.isArray(message.args) ? message.args : [];

      // A refusal obeys the same invoke/send contract as a real dispatch: a
      // send-mode channel is never answered, so refusing one stays silent
      // rather than pushing an unsolicited error frame at a caller that is not
      // listening for a reply.
      const refuse = (code, entry, describe) => {
        if (entry.mode === 'send') return undefined;
        return rawSend(socket, errorPayload(id, code, describe(entry.reason)));
      };
      // A WEB_LOCAL channel arriving here means the browser shim failed to
      // answer it locally. Refusing is deliberate: silently serving
      // clipboard:read-text would hand back the HOST's clipboard.
      if (WEB_LOCAL[method]) {
        return refuse('web_local', WEB_LOCAL[method], (reason) => `${method} is answered in the browser: ${reason}`);
      }
      if (WEB_UNSUPPORTED[method]) {
        return refuse('web_unsupported', WEB_UNSUPPORTED[method], (reason) => `${method} is not available in web mode: ${reason}`);
      }

      const entry = table[method];
      if (!entry) return rawSend(socket, errorPayload(id, 'unknown_method', `unknown method: ${method}`));

      try {
        const result = await entry.handler(connection.event, ...args);
        if (entry.mode === 'send') return undefined;  // fire-and-forget, like ipcRenderer.send
        return rawSend(socket, { id, ok: true, result: result === undefined ? null : result });
      } catch (e) {
        logger.warn(`[web] ${method} threw: ${e && e.message}`);
        if (entry.mode === 'send') return undefined;
        return rawSend(socket, errorPayload(id, 'handler_error', String((e && e.message) || e)));
      }
    },

    /** Push an event to every connected socket. */
    broadcast(event, ...args) {
      for (const socket of connections.keys()) rawSend(socket, { event, args });
    },

    /** Tear every connection down (host shutdown). */
    closeAll() {
      for (const socket of [...connections.keys()]) bridge.removeSocket(socket);
    },
  };

  return bridge;
}

module.exports = { createWsBridge };
