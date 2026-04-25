'use strict';

const WebSocket = require('ws');

function _backoff(attempt) {
  const base = Math.min(30, [1, 2, 5, 10, 30][Math.min(attempt, 4)]);
  const jitter = base * Math.random() * 0.30;
  return (base + jitter) * 1000;
}

class ChatStreamClient {
  constructor() {
    this._ws = null;
    this._cfg = null;
    this._destroyed = false;
    this._reconnectAttempt = 0;
    this._events = [];
    this._drafts = {};
    this._sessions = [];
    this._recentLimit = 500;
    this.connected = false;
    this._emit = null;
  }

  init(cfg, emit) {
    this._cfg = cfg || {};
    this._emit = emit;
    this._recentLimit = this._cfg.chatStream?.recentLimit || 500;
    this._connect();
  }

  snapshot() {
    return {
      connected: this.connected,
      events: this._events.slice(-this._recentLimit),
      drafts: { ...this._drafts },
      sessions: this._sessions.slice(),
    };
  }

  destroy() {
    this._destroyed = true;
    if (this._ws) {
      try { this._ws.terminate(); } catch (_) {}
    }
  }

  _emitSnapshot() {
    if (this._emit) {
      this._emit({
        type: 'snapshot',
        events: this._events.slice(-this._recentLimit),
        drafts: { ...this._drafts },
        sessions: this._sessions.slice(),
      });
    }
  }

  _push(event) {
    this._events.push(event);
    if (this._events.length > this._recentLimit) {
      this._events.splice(0, this._events.length - this._recentLimit);
    }
    if (this._emit) this._emit(event);
  }

  _connect() {
    if (this._destroyed) return;
    const url = this._cfg.chatStream?.url || 'ws://127.0.0.1:7791';
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error('[ChatStream] constructor error:', e.message);
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;
    ws.on('open', () => {
      this.connected = true;
      this._reconnectAttempt = 0;
      ws.send(JSON.stringify({ type: 'hello', client: 'pentacle', subscribe: { all: true } }));
    });
    ws.on('message', (rawData) => {
      let msg;
      try { msg = JSON.parse(rawData.toString()); } catch (_) { return; }
      if (msg.type === 'snapshot' && Array.isArray(msg.events)) {
        this._events = msg.events.slice(-this._recentLimit);
        this._drafts = msg.drafts || {};
        this._sessions = Array.isArray(msg.sessions) ? msg.sessions : [];
        this._emitSnapshot();
      } else if (msg.type === 'session.inventory' && Array.isArray(msg.sessions)) {
        this._sessions = msg.sessions;
        this._emitSnapshot();
      } else if (msg.type === 'chat.event' && msg.event) {
        if (msg.event.kind === 'DRAFT' && msg.event.stream_id) {
          this._drafts[msg.event.stream_id] = msg.event;
        }
        this._push(msg.event);
      } else if (msg.type === 'ping') {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'pong' }));
      }
    });
    ws.on('close', () => {
      this.connected = false;
      this._scheduleReconnect();
    });
    ws.on('error', (err) => {
      console.error('[ChatStream] WS error:', err.message);
    });
  }

  _scheduleReconnect() {
    if (this._destroyed) return;
    const delay = _backoff(this._reconnectAttempt++);
    setTimeout(() => this._connect(), delay);
  }
}

module.exports = new ChatStreamClient();
