// Generic runtime catalog client. The host allowlist is an API convention,
// not a sandbox: installed adapters execute in the renderer's origin.
(function(root) {
  'use strict';
  const HOST_API = 1;
  const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const object = (v, p) => { if (!v || typeof v !== 'object' || Array.isArray(v)) fail(p, 'must be an object'); };
  const fail = (p, m) => { throw new Error(`${p}: ${m}`); };
  const unknown = (v, p, allowed) => { object(v, p); const keys = Object.keys(v).filter(k => !allowed.includes(k)); if (keys.length) fail(p, `unknown keys: ${keys.sort().join(', ')}`); };
  const chars = s => Array.from(s).length;
  function string(v, k, p, max) { const s = v[k]; if (typeof s !== 'string' || !s.trim()) fail(p, 'must be a non-empty string'); if (max && chars(s) > max) fail(p, `must be at most ${max} characters`); return s; }
  function match(v, k, p, re) { if (typeof v[k] !== 'string' || !(v[k].match(re)?.[0] === v[k])) fail(p, `must match ${re.source}`); return v[k]; }
  function integer(v, p, min, max) { if (!Number.isInteger(v) || v < min || v > max) fail(p, `must be an integer ${min}-${max}`); }
  const patterns = {
    version: /^[0-9A-Za-z.+-]{1,64}$/, id: /^[a-z0-9][a-z0-9-]{1,63}$/,
    path: /^web\/[a-z0-9][a-z0-9._-]{0,80}\.(js|css)$/,
    hash: /^[0-9a-f]{64}$/, commit: /^[0-9a-f]{40}$/,
    spec: /^[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$/, prefix: /^[a-z0-9][a-z0-9._-]{0,63}$/,
    stream: /^[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,128}$/,
  };
  function keyFormatWidth(value) {
    if (typeof value !== 'string' || !value) throw new Error('key_format must be a non-empty string');
    const token = /\[(0-9|A-Z)\](?:\{([0-9]{1,2})\})?|([0-9A-Z_-])|(\\\.)/y;
    let at = 0, width = 0;
    while (at < value.length) {
      token.lastIndex = at; const m = token.exec(value);
      if (!m) throw new Error(`key_format: unsupported token at offset ${at}`);
      const count = m[1] && m[2] !== undefined ? Number(m[2]) : 1;
      if (count < 1) throw new Error('key_format: class count must be >= 1');
      width += count; at = token.lastIndex;
    }
    if (width < 4 || width > 32) throw new Error(`key_format: width ${width} outside 4-32`);
    return width;
  }
  function plainHttpUrl(value) {
    if (typeof value !== 'string' || value.length > 2048 || /\s/.test(value) || !/^https?:\/\/[^/]/i.test(value)) return false;
    try {
      const url = new URL(value); const authority = value.split('/')[2];
      return ['http:', 'https:'].includes(url.protocol) && !!url.hostname && !url.username && !url.password && !authority.includes('@') && !authority.includes('\\');
    } catch { return false; }
  }
  // Python json.dumps uses a space after separators; measure the same UTF-8
  // representation rather than admitting oversized entries with compact JSON.
  function serializedBytes(value) {
    function dump(v) {
      if (Array.isArray(v)) return '[' + v.map(dump).join(', ') + ']';
      if (v && typeof v === 'object') return '{' + Object.keys(v).map(k => JSON.stringify(k) + ': ' + dump(v[k])).join(', ') + '}';
      return JSON.stringify(v);
    }
    return new TextEncoder().encode(dump(value)).length;
  }
  function validateReport(v, p) {
    unknown(v, p, ['spec_id', 'asset_id_prefix', 'key_format', 'rev_width', 'producer_stream_id', 'writer_enforced', 'select', 'list', 'history_limit', 'title_template']);
    const spec = match(v, 'spec_id', `${p}.spec_id`, patterns.spec);
    if (spec.split('__').length !== 2) fail(`${p}.spec_id`, 'must be a spec id <repo>__<topic>');
    match(v, 'asset_id_prefix', `${p}.asset_id_prefix`, patterns.prefix);
    try { keyFormatWidth(v.key_format); } catch (e) { fail(`${p}.key_format`, e.message); }
    integer(v.rev_width, `${p}.rev_width`, 0, 3);
    if (own(v, 'producer_stream_id')) match(v, 'producer_stream_id', `${p}.producer_stream_id`, patterns.stream);
    for (const k of ['writer_enforced', 'list']) if (typeof v[k] !== 'boolean') fail(`${p}.${k}`, 'must be a boolean');
    if (v.select !== 'latest') fail(`${p}.select`, "must be 'latest'");
    integer(v.history_limit, `${p}.history_limit`, 1, 100);
    if (own(v, 'title_template')) {
      const t = v.title_template;
      if (typeof t !== 'string' || chars(t) > 80) fail(`${p}.title_template`, 'must be a string of at most 80 characters');
      const bad = [...t.matchAll(/\{([^{}]*)\}/g)].some(m => !['key', 'rev'].includes(m[1]));
      if (bad || (t.match(/\{/g) || []).length !== (t.match(/\}/g) || []).length) fail(`${p}.title_template`, 'only {key} and {rev} placeholders are allowed');
    }
  }
  function validateCatalog(v) {
    const p = 'catalog'; unknown(v, p, ['schema_version', 'catalog_version', 'package', 'requires', 'libs', 'boards']);
    if (v.schema_version !== 1) fail(`${p}.schema_version`, 'must be 1');
    match(v, 'catalog_version', `${p}.catalog_version`, patterns.version);
    unknown(v.package, `${p}.package`, ['repo', 'commit']); string(v.package, 'repo', `${p}.package.repo`, 128); match(v.package, 'commit', `${p}.package.commit`, patterns.commit);
    unknown(v.requires, `${p}.requires`, ['host_api']); integer(v.requires.host_api, `${p}.requires.host_api`, 1, Infinity);
    const libs = own(v, 'libs') ? v.libs : [];
    if (!Array.isArray(libs) || libs.length > 8) fail(`${p}.libs`, 'must be a list of at most 8 entries');
    libs.forEach((lib, i) => { const q = `${p}.libs[${i}]`; unknown(lib, q, ['path', 'sha256']); match(lib, 'path', `${q}.path`, patterns.path); match(lib, 'sha256', `${q}.sha256`, patterns.hash); });
    if (!Array.isArray(v.boards) || v.boards.length > 64) fail(`${p}.boards`, 'must be a list of at most 64 entries');
    const ids = new Set();
    v.boards.forEach((b, i) => {
      const q = `${p}.boards[${i}]`; object(b, q);
      if (serializedBytes(b) > 8192) fail(q, 'entry exceeds 8192 bytes serialized');
      match(b, 'id', `${q}.id`, patterns.id); if (ids.has(b.id)) fail(`${q}.id`, `duplicate board id '${b.id}'`); ids.add(b.id);
      string(b, 'name', `${q}.name`, 64);
      if (own(b, 'description') && (typeof b.description !== 'string' || chars(b.description) > 200)) fail(`${q}.description`, 'must be a string of at most 200 characters');
      const common = ['id', 'name', 'description', 'kind'];
      if (b.kind === 'report') { unknown(b, q, [...common, 'report']); validateReport(b.report, `${q}.report`); }
      else if (b.kind === 'web-adapter') {
        unknown(b, q, [...common, 'web', 'actions', 'poll_interval_ms']); unknown(b.web, `${q}.web`, ['script', 'sha256', 'css', 'css_sha256']);
        match(b.web, 'script', `${q}.web.script`, patterns.path); if (!b.web.script.endsWith('.js')) fail(`${q}.web.script`, 'must be a .js path'); match(b.web, 'sha256', `${q}.web.sha256`, patterns.hash);
        if (own(b.web, 'css') || own(b.web, 'css_sha256')) { match(b.web, 'css', `${q}.web.css`, patterns.path); if (!b.web.css.endsWith('.css')) fail(`${q}.web.css`, 'must be a .css path'); match(b.web, 'css_sha256', `${q}.web.css_sha256`, patterns.hash); }
        if (own(b, 'actions')) {
          if (!Array.isArray(b.actions) || b.actions.some(a => typeof a !== 'string')) fail(`${q}.actions`, 'must be a list of strings');
          const bad = b.actions.filter(a => !['household', 'assistantState', 'assetList', 'assetGet'].includes(a)); if (bad.length) fail(`${q}.actions`, `unknown host actions: ${bad.join(', ')}`);
        }
        if (own(b, 'poll_interval_ms')) integer(b.poll_interval_ms, `${q}.poll_interval_ms`, 2000, 600000);
      } else if (b.kind === 'hosted-view') {
        unknown(b, q, [...common, 'hosted']); unknown(b.hosted, `${q}.hosted`, ['url']); string(b.hosted, 'url', `${q}.hosted.url`);
        if (!plainHttpUrl(b.hosted.url)) fail(`${q}.hosted.url`, 'must be an absolute http(s) URL with a host and no userinfo');
      } else fail(`${q}.kind`, 'must be one of: hosted-view, report, web-adapter');
    });
    if (v.requires.host_api > HOST_API) { const error = new Error(`requires.host_api ${v.requires.host_api} exceeds ${HOST_API}`); error.unsupported = true; throw error; }
    return v;
  }
  function integrity(hash) {
    const bytes = hash.match(/../g).map(h => parseInt(h, 16));
    return 'sha256-' + (typeof btoa === 'function' ? btoa(String.fromCharCode(...bytes)) : Buffer.from(bytes).toString('base64'));
  }
  function fileUrl(version, path) { return `/dashboards/private/${version}/${path}`; }
  function errorCard(container, message, testid = 'dashboard-board-error', state = 'error') {
    container.replaceChildren(); container.dataset.boardState = state;
    const card = container.ownerDocument.createElement('p'); card.dataset.testid = testid; card.setAttribute('role', 'alert'); card.textContent = message; container.appendChild(card); return card;
  }
  function actions(entry, cc, warn = (...args) => console.warn(...args)) {
    const allowed = new Set(entry.actions || []);
    return Object.fromEntries(['assetList', 'assetGet'].map(action => [action, async params => {
      if (!allowed.has(action) || typeof cc?.[action] !== 'function') { warn('[dashboards] action_not_allowed', { id: entry.id, action }); return { ok: false, error: 'action_not_allowed' }; }
      return cc[action](params);
    }]));
  }
  function createLoader(options = {}) {
    const host = options.root || root, cc = options.cc || host.cc, now = options.now || Date.now;
    let storage; try { storage = options.storage === undefined ? host.sessionStorage : options.storage; } catch { storage = null; }
    const memory = new Map(), loadedTags = new Map(), adapters = new Map();
    let generation = 0, activeVersion = null, activeScriptCatalog = null, scriptQueue = Promise.resolve();
    function cached(spec) {
      if (memory.has(spec)) return memory.get(spec);
      try { const record = JSON.parse(storage?.getItem(`dashboard-catalog:${spec}`) || 'null'); if (record && Number.isFinite(record.savedAt)) { validateCatalog(record.catalog); memory.set(spec, record); return record; } } catch {}
      return null;
    }
    function save(spec, catalog) { const record = { catalog, savedAt: now() }; memory.set(spec, record); try { storage?.setItem(`dashboard-catalog:${spec}`, JSON.stringify(record)); } catch {} return record; }
    async function refresh(spec) {
      const token = ++generation;
      if (!spec) return { status: 'unset', catalog: null, cached: false };
      let body;
      try {
        const reply = await cc.assetList({ spec_id: spec });
        if (reply?.ok === false) throw new Error(reply.error || 'asset.list failed');
        const rows = reply?.assets || reply?.result?.assets;
        if (!Array.isArray(rows)) throw new Error('asset.list returned no assets');
        const record = rows.find(r => r?.content_type === 'dashboard-catalog' && r.asset_id === 'dashboard-catalog');
        if (!record || typeof record.stream_id !== 'string' || !record.stream_id) throw new Error('dashboard-catalog asset not found');
        const got = await cc.assetGet({ stream_id: record.stream_id, asset_id: record.asset_id, spec_id: spec });
        if (got?.ok === false) throw new Error(got.error || 'asset.get failed');
        const asset = got?.asset || got?.result?.asset;
        if (!asset || !own(asset, 'body')) throw new Error('asset.get returned no catalog body');
        body = asset.body;
      } catch (error) {
        if (token !== generation) return { status: 'superseded' };
        const record = cached(spec);
        return { status: 'unavailable', message: `Dashboard catalog unavailable: ${error.message || error}`, catalog: record?.catalog || null, cached: !!record, age: record ? Math.max(0, now() - record.savedAt) : null };
      }
      if (token !== generation) return { status: 'superseded' };
      let catalog;
      try { catalog = typeof body === 'string' ? JSON.parse(body) : body; validateCatalog(catalog); }
      catch (error) { return { status: error.unsupported ? 'unsupported' : 'malformed', catalog: null, cached: false, message: `Dashboard catalog unsupported/malformed: ${error.message || error} (catalog ${typeof catalog?.catalog_version === 'string' ? catalog.catalog_version : 'unknown'})` }; }
      save(spec, catalog); return { status: 'ready', catalog, cached: false };
    }
    function loadTag(catalog, path, hash) {
      const url = fileUrl(catalog.catalog_version, path), key = `${url}:${hash}`;
      if (loadedTags.has(key)) return loadedTags.get(key);
      const promise = new Promise((resolve, reject) => {
        const doc = host.document, css = path.endsWith('.css'), tag = doc.createElement(css ? 'link' : 'script');
        if (css) { tag.rel = 'stylesheet'; tag.href = url; tag.dataset.catalogVersion = catalog.catalog_version; tag.disabled = !!activeVersion && activeVersion !== catalog.catalog_version; } else { tag.src = url; tag.async = false; }
        tag.integrity = integrity(hash); tag.crossOrigin = 'anonymous';
        let settled = false;
        const finish = error => { if (settled) return; settled = true; clearTimeout(timer); tag.onload = tag.onerror = null; if (error) { tag.remove(); reject(error); } else resolve(tag); };
        const timer = setTimeout(() => finish(new Error(`load timed out: ${path}`)), options.loadTimeoutMs || 15000);
        tag.onload = () => finish();
        tag.onerror = async () => {
          let missing = false;
          try { if (typeof host.fetch === 'function') missing = (await host.fetch(url, { method: 'HEAD', cache: 'no-store' })).status === 404; } catch {}
          finish(new Error(missing ? `catalog files for version ${catalog.catalog_version} not installed` : `integrity or load error: ${path}`));
        };
        doc.head.appendChild(tag);
      });
      loadedTags.set(key, promise); promise.catch(() => loadedTags.delete(key)); return promise;
    }
    function loadAdapter(catalog, entry) {
      const key = JSON.stringify([catalog.catalog_version, entry.id, entry.web]);
      const task = scriptQueue.then(async () => {
        const identity = JSON.stringify([catalog.catalog_version, catalog.libs || []]);
        if (identity !== activeScriptCatalog) {
          // A version transition (including rollback) must execute its libraries
          // again: old adapter objects can otherwise read the newer globals.
          for (const [tagKey, pending] of loadedTags) if (tagKey.includes('.js:')) {
            pending.then(tag => tag.remove(), () => {}); loadedTags.delete(tagKey);
          }
          adapters.clear(); activeScriptCatalog = identity;
        }
        if (adapters.has(key)) return adapters.get(key);
        const registry = host.DASHBOARDS || (host.DASHBOARDS = []), added = [];
        const previousPush = registry.push, ownPush = own(registry, 'push');
        // Capture registrations without publishing transient entries or restoring
        // an obsolete registry after a concurrent view refresh.
        registry.push = (...entries) => { added.push(...entries); return registry.length + added.length; };
        try {
          for (const lib of catalog.libs || []) await loadTag(catalog, lib.path, lib.sha256);
          if (entry.web.css) await loadTag(catalog, entry.web.css, entry.web.css_sha256);
          await loadTag(catalog, entry.web.script, entry.web.sha256);
          const adapter = added.find(board => board.id === entry.id && typeof board.mount === 'function');
          if (!adapter || added.some(board => board.id !== entry.id)) throw new Error(`script did not register only ${entry.id}`);
          adapters.set(key, adapter); return adapter;
        } catch (error) {
          const scriptKey = `${fileUrl(catalog.catalog_version, entry.web.script)}:${entry.web.sha256}`;
          loadedTags.get(scriptKey)?.then(tag => tag.remove(), () => {}); loadedTags.delete(scriptKey);
          throw error;
        } finally { if (ownPush) registry.push = previousPush; else delete registry.push; }
      });
      scriptQueue = task.catch(() => {}); return task;
    }
    function wrapAdapter(catalog, entry) {
      const board = { ...entry, actions: Object.freeze([...(entry.actions || [])]), catalog: true };
      board.mount = (container, ctx) => {
        container.dataset.boardState = 'loading';
        const refs = { disposed: false, adapter: null, inner: null, container };
        refs.ready = loadAdapter(catalog, entry).then(adapter => {
          if (refs.disposed) return;
          refs.adapter = adapter;
          refs.inner = adapter.mount(container, { ...ctx, actions: actions(entry, cc) });
          container.dataset.boardState = 'ready';
        }).catch(error => { if (!refs.disposed) errorCard(container, `Board failed to load: ${error.message || error}`); });
        return refs;
      };
      board.unmount = refs => { if (!refs) return; refs.disposed = true; try { refs.adapter?.unmount?.(refs.inner); } catch (e) { console.warn('[dashboards] unmount failed', e); } };
      if (entry.poll_interval_ms) {
        board.pollInterval = entry.poll_interval_ms;
        board.pollFn = async refs => {
          await refs.ready; if (refs.disposed || !refs.adapter?.pollFn) return {};
          try { const reply = await refs.adapter.pollFn(refs.inner); if (!refs.disposed && reply?.error) errorCard(refs.container, `Board failed to load: ${reply.error}`); return reply; }
          catch (error) { if (!refs.disposed) errorCard(refs.container, `Board failed to load: ${error.message || error}`); throw error; }
        };
        board.update = (refs, value) => { if (!refs.disposed) { try { refs.adapter?.update?.(refs.inner, value); } catch (e) { errorCard(refs.container, `Board failed to load: ${e.message || e}`); } } };
      }
      return board;
    }
    function merge(result, builtins, dependencies = {}) {
      const boards = [...builtins], errors = [];
      activeVersion = result.catalog?.catalog_version || null;
      host.document?.querySelectorAll('link[data-catalog-version]').forEach(tag => { tag.disabled = tag.dataset.catalogVersion !== activeVersion; });
      if (!result.catalog) return { boards, errors };
      for (const entry of result.catalog.boards) {
        if (boards.some(b => b.id === entry.id)) { errors.push(`Board failed to load: duplicate built-in id ${entry.id}`); continue; }
        try {
          if (entry.kind === 'web-adapter') boards.push(wrapAdapter(result.catalog, entry));
          else if (entry.kind === 'report') boards.push({ ...dependencies.reportBoard.createBoard(entry, { assetList: p => cc.assetList(p), assetGet: p => cc.assetGet(p), renderAsset: dependencies.renderAsset }), catalog: true });
          else {
            const viewer = dependencies.hostedBoard;
            boards.push({ ...entry, catalog: true,
              mount(container, ctx = {}) {
                container.dataset.boardState = 'loading';
                const config = { ...ctx.config, dashboards: { ...ctx.config?.dashboards, modeler3d: { url: entry.hosted.url } } };
                try {
                  const inner = viewer.mount(container, { ...ctx, config });
                  container.querySelector('h1').textContent = entry.name;
                  const card = container.ownerDocument.createElement('p'); card.dataset.testid = 'dashboard-board-error'; card.setAttribute('role', 'alert'); card.hidden = true;
                  inner.shell.appendChild(card);
                  const refs = { inner, observer: null, disposed: false };
                  const sync = () => {
                    if (refs.disposed) return;
                    const state = inner.shell.dataset.modelerState;
                    container.dataset.boardState = state === 'loaded' ? 'ready' : state === 'loading' ? 'loading' : 'error';
                    const frame = container.querySelector('iframe'); if (frame) frame.title = `${entry.name} viewer`;
                    card.hidden = container.dataset.boardState !== 'error';
                    card.textContent = card.hidden ? '' : `Board failed to load: ${inner.shell.querySelector('.modeler-status')?.textContent || 'viewer unavailable'}`;
                  };
                  refs.observer = new container.ownerDocument.defaultView.MutationObserver(sync);
                  refs.observer.observe(inner.shell, { attributes: true, attributeFilter: ['data-modeler-state'] });
                  sync(); return refs;
                } catch (e) { errorCard(container, `Board failed to load: ${e.message || e}`); return null; }
              }, unmount: refs => { if (refs) { refs.disposed = true; refs.observer?.disconnect(); viewer.unmount(refs.inner); } } });
          }
        } catch (error) { boards.push({ ...entry, catalog: true, mount: container => errorCard(container, `Board failed to load: ${error.message || error}`) }); }
      }
      return { boards, errors };
    }
    return { refresh, merge, loadTag, loadAdapter, cancel: () => { generation++; } };
  }
  const api = { HOST_API, validateCatalog, keyFormatWidth, plainHttpUrl, integrity, fileUrl, actions, errorCard, createLoader };
  root.DashboardCatalogLoader = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
