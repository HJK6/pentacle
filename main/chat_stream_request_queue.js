'use strict';

// Match the daemon's wire-type pool routing, never an IPC/display prefix.
const BYPASS = new Set(['send', 'notification.resolve', 'notification.resolve_by_dedup',
  'prompt.answer', 'voice_answer.answer', 'await_report', 'await_spawn', 'transcribe_blob',
  'ping', 'spawn_cancel', 'daemon.stats']);
const BACKGROUND = new Set(['asset.list', 'asset.get', 'asset.comments.list',
  'prompt.list', 'assistant.binding', 'request_stream_events', 'spawn_catalog_get',
  'specs.list', 'specs.get', 'specs.capabilities', 'notification.list', 'household.snapshot',
  'consent.status', 'send.receipt.get']);
const MiB = 1024 * 1024;

class OrdinaryRequestQueue {
  constructor() { this.waiting = []; this.active = new Set(); this.closed = false; }

  add(job) {
    if (this.closed) return 'Stream disconnected';
    if (job.bytes > 6 * MiB) return 'Request exceeds the client request window';
    const background = this.waiting.filter(x => x.background);
    if (this.waiting.length >= 64 || this.waiting.reduce((n, x) => n + x.bytes, 0) + job.bytes > 16 * MiB
      || (job.background && (background.length >= 48 || background.reduce((n, x) => n + x.bytes, 0) + job.bytes > 8 * MiB))) {
      return 'Server is busy. Try sending again.';
    }
    job.state = 'waiting';
    this.waiting.push(job);
    this.drain();
    return null;
  }

  drain() {
    if (this.closed) return;
    while (this.active.size < 6) {
      const active = [...this.active];
      const bytes = active.reduce((n, x) => n + x.bytes, 0);
      const backgroundCount = active.filter(x => x.background).length;
      const fits = x => bytes + x.bytes <= 6 * MiB && (!x.background || backgroundCount < 4);
      let index = this.waiting.findIndex(x => !x.background && fits(x));
      if (index < 0) index = this.waiting.findIndex(fits);
      if (index < 0) return;
      const [job] = this.waiting.splice(index, 1);
      this.active.add(job);
      job.state = 'active';
      job.run();
    }
  }

  release(job) {
    this.active.delete(job);
    const index = this.waiting.indexOf(job);
    if (index >= 0) this.waiting.splice(index, 1);
    job.state = 'released';
    // Let reply handlers finish before admitting another RPC.
    queueMicrotask(() => this.drain());
  }

  close() {
    this.closed = true;
    for (const job of [...this.waiting, ...this.active]) job.state = 'closed';
    this.waiting = [];
    this.active.clear();
  }
}

module.exports = { OrdinaryRequestQueue, BYPASS, BACKGROUND };
