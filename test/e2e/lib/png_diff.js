'use strict';
const zlib = require('zlib');

// Minimal PNG reader for Chrome screenshots (8-bit RGB/RGBA, not interlaced)
// and a pixel diff that reports what changed outside named allowed regions.
function decodePng(buffer) {
  if (buffer.readUInt32BE(0) !== 0x89504e47) throw new Error('not a PNG');
  let offset = 8; let header = null; const idat = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString('ascii', offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') header = { width: data.readUInt32BE(0), height: data.readUInt32BE(4), depth: data[8], color: data[9], interlace: data[12] };
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    offset += 12 + length;
  }
  if (!header || header.depth !== 8 || header.interlace !== 0 || ![2, 6].includes(header.color)) {
    throw new Error(`unsupported PNG ${JSON.stringify(header)}`);
  }
  const { width, height } = header;
  const bpp = header.color === 6 ? 4 : 3;
  const stride = width * bpp;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const out = Buffer.alloc(width * height * 4);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const line = Buffer.from(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)));
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? line[x - bpp] : 0; const b = prev[x]; const c = x >= bpp ? prev[x - bpp] : 0;
      let predictor = 0;
      if (filter === 1) predictor = a;
      else if (filter === 2) predictor = b;
      else if (filter === 3) predictor = (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c; const pa = Math.abs(p - a); const pb = Math.abs(p - b); const pc = Math.abs(p - c);
        predictor = pa <= pb && pa <= pc ? a : (pb <= pc ? b : c);
      } else if (filter !== 0) throw new Error(`bad PNG filter ${filter}`);
      line[x] = (line[x] + predictor) & 255;
    }
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4;
      out[o] = line[x * bpp]; out[o + 1] = line[x * bpp + 1]; out[o + 2] = line[x * bpp + 2];
      out[o + 3] = bpp === 4 ? line[x * bpp + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

// Pixels that differ by more than `tolerance` in any channel. Regions are
// { name, x, y, w, h } in CSS pixels at scale 1; each reports its own count,
// and everything outside them is summarized with a bounding box.
function diffImages(a, b, regions = [], { tolerance = 0 } = {}) {
  if (a.width !== b.width || a.height !== b.height) {
    return { sizeMismatch: true, a: [a.width, a.height], b: [b.width, b.height] };
  }
  const byRegion = Object.fromEntries(regions.map(r => [r.name, 0]));
  let differing = 0; let outside = 0;
  const box = { x0: Infinity, y0: Infinity, x1: -1, y1: -1 };
  for (let y = 0; y < a.height; y++) {
    for (let x = 0; x < a.width; x++) {
      const o = (y * a.width + x) * 4;
      if (Math.abs(a.data[o] - b.data[o]) <= tolerance && Math.abs(a.data[o + 1] - b.data[o + 1]) <= tolerance
        && Math.abs(a.data[o + 2] - b.data[o + 2]) <= tolerance) continue;
      differing++;
      const region = regions.find(r => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
      if (region) { byRegion[region.name]++; continue; }
      outside++;
      box.x0 = Math.min(box.x0, x); box.y0 = Math.min(box.y0, y); box.x1 = Math.max(box.x1, x); box.y1 = Math.max(box.y1, y);
    }
  }
  return { differing, outside, byRegion,
    outsideBox: outside ? { x: box.x0, y: box.y0, w: box.x1 - box.x0 + 1, h: box.y1 - box.y0 + 1 } : null };
}

module.exports = { decodePng, diffImages };
