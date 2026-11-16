'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const esbuild = require('esbuild');
const { sha256, contained, safePath, LIMITS, readJson, atomicJson } = require('./design_library.cjs');

const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
const STORAGE = `for(const name of ['localStorage','sessionStorage']){const data=new Map();Object.defineProperty(window,name,{configurable:false,value:Object.freeze({getItem(k){k=String(k);return data.has(k)?data.get(k):null},setItem(k,v){data.set(String(k),String(v))},removeItem(k){data.delete(String(k))},clear(){data.clear()},key(i){return [...data.keys()][Number(i)>>>0]??null},get length(){return data.size}})})}`;
const MIME = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.avif': 'image/avif', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf', '.mp4': 'video/mp4', '.webm': 'video/webm' };
function escapeScript(value) { return value.replace(/<\/script/gi, '<\\/script'); }
function scriptPolicy(source, file) {
  // Conservative admission for curated exports, not a security proof for
  // arbitrary JS. Runtime browser verification separately observes journeys.
  const denied = [
    /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|Worker|SharedWorker|importScripts|sendBeacon)\s*\(/,
    /\bimport\s*\(/, /\b(?:eval|Function)\s*\(/,
    /\b(?:window\s*\.\s*)?open\s*\(/,
    /\b(?:window\s*\.\s*|top\s*\.\s*|parent\s*\.\s*)?location\s*(?:=(?!=)|\.\s*(?:href\s*=(?!=)|assign\s*\(|replace\s*\())/,
    /\b(?:window\s*\.\s*)?claude\s*(?:\.|\[)/, /\bomelette\s*(?:\?\.|\.|\[)/,
    /\bwindow\s*\[\s*['"](?:claude|omelette|location)['"]/, /\bserviceWorker\b/,
    /\b(?:createElement|createElementNS)\s*\(\s*['"](?:script|iframe|object|embed|link)['"]/i,
  ];
  // esbuild removes comments before this scan, so documentation words cannot
  // accidentally grant or deny executable operations.
  if (denied.some(re => re.test(source))) throw new Error(`${file}: unsupported network, editor, navigation or dynamic code operation; supply a reviewed deterministic adapter or a screenshot fallback`);
}

async function prepareDocument({ sourceRoot, entry, profile = {} }) {
  const used = new Map(), adaptations = [];
  function resource(ref, base) {
    if (/^https?:\/\//i.test(ref)) {
      const dep = profile.assets?.[ref];
      if (!dep || !/^[a-f0-9]{64}$/.test(dep.sha256) || !dep.version || !dep.license || !dep.path) throw new Error(`${ref}: dependency bytes/version/license not pinned`);
      let bytes;
      try { bytes = fs.readFileSync(dep.path); } catch { throw new Error(`${ref}: pinned dependency cache unavailable`); }
      if (bytes.length > LIMITS.file || sha256(bytes) !== dep.sha256) throw new Error(`${ref}: dependency hash/size mismatch`);
      used.set(ref, { sha256: dep.sha256, size: bytes.length, version: dep.version, license: dep.license, content_type: dep.content_type });
      return { bytes, name: ref, trusted: dep.trusted_script === true, mime: dep.content_type };
    }
    if (/^[a-z][a-z0-9+.-]*:|^\/\//i.test(ref) || ref.startsWith('/')) throw new Error(`${ref}: unsupported reference`);
    const decoded = decodeURIComponent(ref.split(/[?#]/)[0]);
    if (decoded.includes('\\')) throw new Error(`${ref}: invalid asset path`);
    const file = path.posix.normalize(path.posix.join(path.posix.dirname(base), decoded));
    safePath(file);
    let bytes;
    try { bytes = fs.readFileSync(contained(sourceRoot, file)); } catch { throw new Error(`${file}: missing or unsafe referenced local asset`); }
    return { bytes, name: file, trusted: false, mime: MIME[path.extname(file).toLowerCase()] };
  }
  function adapt(source, file) {
    for (const adapter of profile.adapters || []) {
      if (adapter.path !== file) continue;
      if (sha256(source) !== adapter.sha256 || typeof adapter.replacement !== 'string' || !adapter.reason || !adapter.review_ref) throw new Error(`${file}: invalid or stale reviewed adapter`);
      adaptations.push({ path: file, original_sha256: adapter.sha256, replacement_sha256: sha256(adapter.replacement), reason: adapter.reason, review_ref: adapter.review_ref, mocked: !!adapter.mocked });
      source = adapter.replacement;
    }
    return source;
  }
  function data(ref, base) {
    if (ref.startsWith('data:') || ref.startsWith('#')) return ref;
    const r = resource(ref, base);
    if (!r.mime || !/^(?:image|font|video|audio)\//.test(r.mime)) throw new Error(`${ref}: unsupported embedded media type`);
    if (r.mime === 'image/svg+xml') {
      // SVG images cannot smuggle external resources into the prepared page.
      const svg = r.bytes.toString('utf8');
      if (/<script\b|\bon\w+\s*=|(?:href|src)\s*=\s*["'](?!#|data:)|url\(\s*["']?(?!#|data:)/i.test(svg)) throw new Error(`${ref}: SVG contains active/external references`);
    }
    return `data:${r.mime};base64,${r.bytes.toString('base64')}`;
  }
  function css(text, base, depth = 0) {
    if (depth > 8) throw new Error(`${base}: CSS dependency recursion`);
    text = text.replace(/@import\s+(?:url\(\s*)?['"]([^'"]+)['"]\s*\)?\s*;/gi, (_, ref) => { const r = resource(ref, base); return css(r.bytes.toString('utf8'), r.name, depth + 1); });
    text = text.replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^\s)]+))\s*\)/gi, (_, a, b, c) => `url("${data(a ?? b ?? c, base)}")`);
    if (/@import\b|expression\s*\(/i.test(text)) throw new Error(`${base}: unresolved/unsupported CSS`);
    return text;
  }
  let html = fs.readFileSync(contained(sourceRoot, entry), 'utf8');
  const dom = new JSDOM(html); const document = dom.window.document;
  if (document.querySelector('x-dc,script[type="text/x-dc"]')) throw new Error(`${entry}: custom design editor runtime is unsupported; use the retained screenshot`);
  for (const el of document.querySelectorAll('base,meta[http-equiv],iframe,object,embed,applet')) el.remove();
  for (const link of [...document.querySelectorAll('link')]) {
    if (link.rel === 'stylesheet') {
      const r = resource(link.getAttribute('href'), entry), style = document.createElement('style');
      style.textContent = css(r.bytes.toString('utf8'), r.name); link.replaceWith(style);
    } else link.remove();
  }
  for (const el of document.querySelectorAll('style')) el.textContent = css(el.textContent, entry);
  for (const el of document.querySelectorAll('*')) {
    for (const attr of [...el.attributes]) {
      if (attr.name.startsWith('on')) {
        const compiled = (await esbuild.transform(attr.value, { loader: 'js', minifyWhitespace: true })).code; scriptPolicy(compiled, entry);
        // CSP inline handlers are intentionally unsupported: do not turn a
        // document that depended on them into a falsely interactive preview.
        throw new Error(`${entry}: inline event handlers unsupported; use a reviewed adapter`);
      }
      if (attr.name === 'style') el.setAttribute('style', css(attr.value, entry));
      if (['src', 'poster', 'xlink:href'].includes(attr.name) && el.tagName !== 'SCRIPT') el.setAttribute(attr.name, data(attr.value, entry));
      if (attr.name === 'srcset') {
        if (attr.value.includes('data:')) throw new Error(`${entry}: mixed srcset requires explicit preparation`);
        el.setAttribute('srcset', attr.value.split(',').map(candidate => { const [ref, ...size] = candidate.trim().split(/\s+/); return data(ref, entry) + (size.length ? ' ' + size.join(' ') : ''); }).join(', '));
      }
      if (['action', 'formaction', 'ping', 'target', 'download'].includes(attr.name)) el.removeAttribute(attr.name);
      if (attr.name === 'href' && el.tagName !== 'LINK' && !attr.value.startsWith('#')) { el.removeAttribute('href'); el.setAttribute('aria-disabled', 'true'); }
    }
  }
  for (const script of [...document.querySelectorAll('script')]) {
    const src = script.getAttribute('src');
    if (src && /^https:\/\/unpkg\.com\/@babel\/standalone@7\.29\.0\/babel\.min\.js$/.test(src)) { script.remove(); continue; }
    if (script.type && !['text/javascript', 'application/javascript', 'text/babel'].includes(script.type)) {
      if (script.type === 'application/json') continue;
      throw new Error(`${entry}: unsupported script type ${script.type}`);
    }
    const r = src ? resource(src, entry) : { bytes: Buffer.from(script.textContent), name: entry, trusted: false };
    let source = adapt(r.bytes.toString('utf8'), r.name);
    const compiled = (await esbuild.transform(source, { loader: script.type === 'text/babel' || /\.[jt]sx$/.test(r.name) ? 'jsx' : 'js', target: 'es2020', minifyWhitespace: true, legalComments: 'none' })).code;
    if (!r.trusted) scriptPolicy(compiled, r.name);
    const replacement = document.createElement('script'); replacement.textContent = escapeScript(compiled); script.replaceWith(replacement);
  }
  const policy = document.createElement('meta'); policy.httpEquiv = 'Content-Security-Policy'; policy.content = CSP;
  const storage = document.createElement('script'); storage.textContent = STORAGE;
  document.head.prepend(storage); document.head.prepend(policy);
  html = dom.serialize(); dom.window.close();
  if (Buffer.byteLength(html) > LIMITS.prepared) throw new Error(`${entry}: prepared page exceeds 32 MiB`);
  return { html, dependencies: [...used].map(([url, v]) => ({ url, ...v })), adaptations };
}

async function prepareRevision(dir) {
  const file = path.join(dir, 'manifest.json'), manifest = readJson(file);
  if (manifest.revision_id) throw new Error('immutable revision; import an editing copy instead');
  fs.mkdirSync(path.join(dir, 'prepared'), { recursive: true });
  const dependencies = new Map();
  for (const view of manifest.views) {
    delete view.prepared; delete view.screenshots; delete view.generated;
    try {
      if (/\.(png|jpe?g|webp)$/i.test(view.entry)) {
        try { contained(path.join(dir, 'source'), view.entry); } catch { throw new Error(`${view.entry}: missing or unsafe image entry`); }
        view.screenshots = [{ name: view.title, path: `source/${view.entry}` }];
        view.compatibility = { status: 'screenshot/video-only', reason: 'Retained original image reference.', fallback: `source/${view.entry}` };
        continue;
      }
      const result = await prepareDocument({ sourceRoot: path.join(dir, 'source'), entry: view.entry, profile: manifest.profile.dependencies });
      view.prepared = `prepared/${view.id}.html`; fs.writeFileSync(path.join(dir, view.prepared), result.html);
      view.adaptations = result.adaptations;
      result.dependencies.forEach(d => dependencies.set(d.url, d));
      view.compatibility = { status: result.adaptations.length ? 'offline-with-mocks/limitations' : 'offline-interactive', reason: result.adaptations.length ? result.adaptations.map(a => a.reason).join('; ') : 'Prepared offline; browser capture required before publication.' };
    } catch (error) {
      const fallback = view.fallback;
      if (fallback) {
        safePath(fallback); contained(path.join(dir, 'source'), fallback);
        if (!/\.(png|jpe?g|webp)$/i.test(fallback)) throw new Error(`${view.id}: fallback must be an image`);
        view.screenshots = [{ name: 'Imported reference', path: `source/${fallback}` }];
        view.compatibility = { status: 'screenshot/video-only', reason: error.message, fallback: `source/${fallback}` };
      } else view.compatibility = { status: 'incomplete', reason: error.message, fallback: null };
    }
  }
  manifest.dependency_inventory = [...dependencies.values()]; manifest.state = 'prepared';
  atomicJson(file, manifest); return manifest;
}
module.exports = { CSP, STORAGE, scriptPolicy, prepareDocument, prepareRevision };
