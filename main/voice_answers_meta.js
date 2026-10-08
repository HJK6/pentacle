'use strict';

// Closed host whitelist for services/chat-stream-v2/voice_answers.py. The
// packet requires numeric version === 1 (Python also compares True == 1).
const ID_FIELDS = ['key', 'question_id', 'notification_id', 'producer_stream_id', 'surface_stream_id'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const length = value => [...value].length; // Python len counts code points, not UTF-16 units.
const strip = value => value.replace(/^[\p{White_Space}\u001c-\u001f]+|[\p{White_Space}\u001c-\u001f]+$/gu, '');
function id(value) {
  return typeof value === 'string' && length(value) <= 200 && strip(value) ? strip(value) : null;
}
function seconds(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  if (Object.is(value, -0)) return -0;
  // toFixed rounds the actual binary float, like Python round(x, 3), except
  // exact halfway ties. Those representable ties are odd multiples of 1/16;
  // use integer arithmetic there to reproduce Python's ties-to-even rule.
  const sixteenths = value * 16;
  if (Number.isSafeInteger(sixteenths) && sixteenths % 2 === 1) {
    const floor = BigInt(sixteenths) * 125n / 2n;
    return Number(`${floor + floor % 2n}e-3`);
  }
  return Number(value.toFixed(3));
}

function validateVoiceAnswers(raw) {
  if (!object(raw) || raw.version !== 1 || !Array.isArray(raw.items)
    || raw.items.length < 1 || raw.items.length > 20) return null;
  const recordingId = id(raw.recording_id);
  const blobSha = id(raw.blob_sha);
  const durationS = seconds(raw.duration_s);
  if (recordingId === null || blobSha === null || durationS === null) return null;
  const keys = new Set();
  const items = [];
  for (const item of raw.items) {
    if (!object(item) || !object(item.segment)) return null;
    const fields = {};
    for (const name of ID_FIELDS) {
      fields[name] = id(item[name]);
      if (fields[name] === null) return null;
    }
    if (keys.has(fields.key)) return null;
    keys.add(fields.key);
    const start = seconds(item.segment.start_s);
    const end = seconds(item.segment.end_s);
    if (start === null || end === null || end < start) return null;
    const prompt = Object.hasOwn(item, 'prompt') ? item.prompt : '';
    if (typeof prompt !== 'string' || length(prompt) > 2000) return null;
    items.push({ ...fields, prompt, segment: { start_s: start, end_s: end } });
  }
  return { version: 1, recording_id: recordingId, blob_sha: blobSha, duration_s: durationS, items };
}

module.exports = { validateVoiceAnswers };
