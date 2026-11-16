'use strict';

// Minimal posix `path` for the browser bundle. The renderer's whole use of the
// module is `path.join(__dirname, '..')` for the config root, plus defensive
// basename/dirname/extname calls; nothing here touches the filesystem.

function normalize(input) {
  const isAbsolute = input.startsWith('/');
  const out = [];
  for (const part of input.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else if (!isAbsolute) out.push('..');
      continue;
    }
    out.push(part);
  }
  const joined = out.join('/');
  if (isAbsolute) return '/' + joined;
  return joined || '.';
}

function join(...parts) {
  const filtered = parts.filter((p) => typeof p === 'string' && p.length);
  if (!filtered.length) return '.';
  return normalize(filtered.join('/'));
}

function dirname(p) {
  const norm = normalize(String(p));
  const i = norm.lastIndexOf('/');
  if (i < 0) return '.';
  if (i === 0) return '/';
  return norm.slice(0, i);
}

function basename(p, ext) {
  const norm = normalize(String(p));
  const base = norm.slice(norm.lastIndexOf('/') + 1);
  if (ext && base.endsWith(ext) && base !== ext) return base.slice(0, -ext.length);
  return base;
}

function extname(p) {
  const base = basename(p);
  const i = base.lastIndexOf('.');
  return i <= 0 ? '' : base.slice(i);
}

function resolve(...parts) {
  let out = '';
  for (const part of parts) {
    if (typeof part !== 'string' || !part) continue;
    out = part.startsWith('/') ? part : (out ? `${out}/${part}` : part);
  }
  return normalize(out.startsWith('/') ? out : `/${out}`);
}

const posix = { join, dirname, basename, extname, resolve, normalize, sep: '/' };
module.exports = { ...posix, posix, win32: posix, default: posix };
