'use strict';

// Self-contained so the same oracle runs in Node controls and the real renderer.
function createTypingProbe({ now = () => performance.now(), count = 240, firstCodePoint = 0x4e00 } = {}) {
  if (!Number.isInteger(count) || count < 1 || firstCodePoint < 0x4e00 || firstCodePoint + count > 0xa000) {
    throw new Error('probe requires unique printable CJK keystrokes');
  }
  const decoder = new TextDecoder();
  const pending = new Map();
  const records = [];
  let duplicates = 0;
  const seen = new Set();
  return {
    records,
    send() {
      if (records.length >= count) throw new Error('probe population exhausted');
      const token = String.fromCodePoint(firstCodePoint + records.length);
      const record = { id: records.length, token, sentAt: now(), receivedAt: null, parsedAt: null, renderedAt: null };
      records.push(record);
      pending.set(token, record);
      return token;
    },
    receive(data) {
      const text = typeof data === 'string' ? data : decoder.decode(data, { stream: true });
      const matched = [];
      const receivedAt = now();
      for (const token of text) {
        const record = pending.get(token);
        if (!record) { if (seen.has(token)) duplicates += 1; continue; }
        record.receivedAt = receivedAt;
        pending.delete(token);
        seen.add(token);
        matched.push(record);
      }
      return matched;
    },
    parsed(matched) { for (const record of matched) record.parsedAt = now(); },
    rendered() {
      const renderedAt = now();
      for (const record of records) {
        if (record.parsedAt !== null && record.renderedAt === null) record.renderedAt = renderedAt;
      }
    },
    result() {
      const percentile = (values, p) => {
        values.sort((a, b) => a - b);
        return values.length ? values[Math.ceil(values.length * p) - 1] : null;
      };
      const stats = (end, start) => {
        const values = records.filter(r => r[end] !== null && r[start] !== null).map(r => r[end] - r[start]);
        return { count: values.length, p50Ms: percentile(values, .5), p95Ms: percentile(values, .95), p99Ms: percentile(values, .99), maxMs: values.length ? Math.max(...values) : null };
      };
      return { expected: count, sent: records.length, received: records.length - pending.size,
        missing: pending.size, unsent: count - records.length, duplicates,
        receive: stats('receivedAt', 'sentAt'), parseDelay: stats('parsedAt', 'receivedAt'),
        renderDelay: stats('renderedAt', 'receivedAt'), render: stats('renderedAt', 'sentAt'), records,
        // tmux may redraw old glyphs; duplicate observations never acknowledge a send.
        complete: records.length === count && pending.size === 0 };
    },
  };
}

module.exports = { createTypingProbe };
