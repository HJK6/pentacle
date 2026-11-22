'use strict';

const fs = require('node:fs/promises');

function validScore(score) {
  return score && typeof score.id === 'string' && score.id.trim()
    && typeof score.name === 'string' && score.name.trim()
    && Number.isSafeInteger(score.defaultGapMs) && score.defaultGapMs > 0
    && Array.isArray(score.notes) && score.notes.every((note, index) =>
      note && Number.isInteger(note.midi) && note.midi >= 24 && note.midi <= 83
      && Number.isSafeInteger(note.durationMs) && note.durationMs > 0
      && Number.isSafeInteger(note.gapMs) && note.gapMs >= 0 && (index > 0 || note.gapMs === 0));
}

async function serveFeaturedScore(req, res, file) {
  const headers = { 'cache-control': 'private, no-store', 'content-type': 'application/json; charset=utf-8' };
  if (req.method !== 'GET') {
    res.writeHead(405, { ...headers, allow: 'GET' }).end();
    return;
  }
  if (!file) {
    res.writeHead(404, headers).end();
    return;
  }
  try {
    const score = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!validScore(score)) throw new Error('Invalid score');
    const { id, name, defaultGapMs } = score;
    const notes = score.notes.map(({ midi, durationMs, gapMs }) => ({ midi, durationMs, gapMs }));
    res.writeHead(200, headers).end(JSON.stringify({ id, name, defaultGapMs, notes }));
  } catch {
    res.writeHead(503, headers).end(JSON.stringify({ error: 'The featured composition could not be loaded.' }));
  }
}

module.exports = { serveFeaturedScore };
