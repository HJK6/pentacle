// ── Pi Control Dashboard ──────────────────────────────────────
// Push-driven dashboard for switching tailnet Pi display profiles.

(function(root) {
'use strict';

const FORECLOSURE_PROFILE_ID = 'foreclosure-live';
const AGENT_OUTPUT_PROFILE_ID = 'agent-output';
const SPECS_PROFILE_ID = 'specs';
const CONTROL_ENVELOPE_ID = 'bart.control';
const FORECLOSURE_DASHBOARD_ID = 'foreclosure-pipeline';
const SPECS_DASHBOARD_ID = 'specs';
const DISAMBIGUATION_LABEL = 'Controls what the Pi displays — does not control the foreclosure pipeline';
const SPECS_DISAMBIGUATION_LABEL = 'Controls what the Pi displays — opens the spec on the wall display, not the desktop dashboard';
const MAX_RECONNECT_SECONDS = 30;
const VIRTUAL_ROW_HEIGHT = 46;
const VIRTUAL_OVERSCAN = 8;

function loadAssetRenderer(root) {
  if (root && root.PentacleAssetRender) return root.PentacleAssetRender;
  return browserRequire('../asset_render') || browserRequire('./asset_render') || browserRequire('./renderer/asset_render');
}

const assetRenderer = loadAssetRenderer(root);
const escapeHtml = assetRenderer.escapeHtml;
const renderMarkdown = assetRenderer.renderMarkdownHtml;
const renderAsset = assetRenderer.renderAsset;

function expandHome(filePath, osMod) {
  if (!filePath) return filePath;
  if (!String(filePath).startsWith('~')) return filePath;
  const home = osMod && typeof osMod.homedir === 'function' ? osMod.homedir() : '';
  return String(filePath).replace(/^~(?=$|\/|\\)/, home);
}

function readTokenFile(filePath, fsMod, osMod) {
  if (!filePath || !fsMod || typeof fsMod.readFileSync !== 'function') return '';
  try {
    return fsMod.readFileSync(expandHome(filePath, osMod), 'utf8').trim();
  } catch (_) {
    return '';
  }
}

function browserRequire(name) {
  try {
    if (typeof require === 'function') return require(name);
  } catch (_) {}
  return null;
}

function loadRuntime(win) {
  const override = win && win.__PI_CONTROL_TEST_RUNTIME;
  if (override) return { ...override };

  const fsMod = browserRequire('fs');
  const osMod = browserRequire('os');
  const pathMod = browserRequire('path');
  let cfg = {};
  try {
    const loader = browserRequire('../../config-loader') || browserRequire('../config-loader');
    const base = pathMod && typeof __dirname !== 'undefined'
      ? pathMod.join(__dirname, '..', '..')
      : undefined;
    if (loader && typeof loader.loadConfig === 'function') cfg = loader.loadConfig(base).config || {};
  } catch (_) {
    cfg = {};
  }

  // Prefer the dashboardHub config preload.js already loaded into window.HOST.
  // require() resolution from a <script src> renderer is brittle across asar
  // packaging; window.HOST.dashboardHubConfig is always populated by preload
  // when the host config has a dashboardHub section.
  const hubCfg = (win && win.HOST && win.HOST.dashboardHubConfig)
    || cfg.dashboardHub
    || {};
  const readTokenPath = hubCfg.readTokenPath || '~/.dashboard-hub/read-token';
  const writeTokenPath = hubCfg.writeTokenPath || '~/.dashboard-hub/write-token';
  return {
    hubUrl: String(hubCfg.url || '').replace(/\/+$/, ''),
    readToken: readTokenFile(readTokenPath, fsMod, osMod),
    writeToken: readTokenFile(writeTokenPath, fsMod, osMod),
    fetch: win && win.fetch ? win.fetch.bind(win) : (typeof fetch === 'function' ? fetch : null),
    EventSource: win && win.EventSource ? win.EventSource : (typeof EventSource !== 'undefined' ? EventSource : null),
    // In-DOM, promise-based confirm (renderer/confirm_dialog.js, loaded as a
    // global). NOT native window.confirm() -- that opens Chromium's native
    // dialog manager and traps the Space key on Windows/Electron after dismiss.
    // Returns Promise<boolean>; call sites await it.
    confirmAction: (msg) => {
      const fn = (win && typeof win.confirmDialog === 'function') ? win.confirmDialog : null;
      if (fn) return fn(msg, { doc: win && win.document });
      return Promise.resolve(true);
    },
    setTimeout: win && win.setTimeout ? win.setTimeout.bind(win) : setTimeout,
    clearTimeout: win && win.clearTimeout ? win.clearTimeout.bind(win) : clearTimeout,
    now: () => Date.now(),
  };
}

function maskTokenInUrl(url) {
  return String(url || '').replace(/([?&]token=)[^&]+/g, '$1***');
}

function fmtDate(value) {
  if (!value) return '--';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function sortOutputs(outputs) {
  return outputs.slice().sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
}

function normalizeOutputs(outputs) {
  const byId = new Map();
  for (const output of Array.isArray(outputs) ? outputs : []) {
    if (output && output.output_id) byId.set(String(output.output_id), output);
  }
  return sortOutputs(Array.from(byId.values()));
}

function outputSearchText(output) {
  return [
    output.title,
    output.agent_id,
    output.content_type,
    ...(Array.isArray(output.tags) ? output.tags : []),
  ].join(' ').toLowerCase();
}

function isEventBefore(ts, baselineMs) {
  if (!ts || !baselineMs) return false;
  const ms = new Date(ts).getTime();
  return Number.isFinite(ms) && ms < baselineMs;
}

function currentDevice(state) {
  return state.devices.find((d) => d.device_id === state.selectedDeviceId) || state.devices[0] || null;
}

function activeProfileForSelectedDevice(state) {
  const device = currentDevice(state);
  return device ? device.profile_id : null;
}

function authHeader(token) {
  return { Authorization: `Bearer ${token || ''}` };
}

function hubUrl(runtime, path) {
  if (!runtime.hubUrl) return path;
  return `${runtime.hubUrl}${path}`;
}

async function hubFetch(runtime, path, options) {
  if (!runtime.fetch) throw new Error('fetch unavailable');
  if (!runtime.hubUrl) throw new Error('dashboardHub.url missing');
  const token = options && options.write ? runtime.writeToken : runtime.readToken;
  if (!token) throw new Error(options && options.write ? 'write token missing' : 'read token missing');
  const init = {
    method: (options && options.method) || 'GET',
    headers: {
      ...authHeader(token),
      ...(options && options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  };
  if (options && options.body) init.body = JSON.stringify(options.body);
  const response = await runtime.fetch(hubUrl(runtime, path), init);
  if (!response || !response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body && body.error ? `: ${body.error}` : '';
    } catch (_) {}
    throw new Error(`hub request failed ${response ? response.status : 'unknown'}${detail}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function updateLiveState(refs, state, connected, detail) {
  state.connected = !!connected;
  refs.liveDot.className = `pi-control-live-dot ${connected ? 'live' : 'offline'}`;
  refs.liveText.textContent = connected ? 'live' : (detail || 'reconnecting');
  if (typeof renderStalenessBadge === 'function') {
    renderStalenessBadge(refs.staleness, {
      _transport_stale: !connected,
      _data_stale: false,
      _updated_at: new Date().toISOString(),
      _age_sec: 0,
    });
  } else {
    refs.staleness.textContent = connected ? 'connected' : (detail || 'reconnecting');
  }
}

function renderDevices(refs, state) {
  const selected = currentDevice(state);
  refs.deviceSelect.innerHTML = '';
  for (const device of state.devices) {
    const opt = refs.root.ownerDocument.createElement('option');
    opt.value = device.device_id;
    opt.textContent = `${device.device_id} · ${device.profile_id || 'unknown'} · ${fmtDate(device.updated_at)}`;
    refs.deviceSelect.appendChild(opt);
  }
  refs.deviceSelect.value = selected ? selected.device_id : '';
  refs.deviceMeta.textContent = selected
    ? `${selected.profile_id || 'unknown'} · updated ${fmtDate(selected.updated_at)}`
    : 'No Pi devices returned by hub';
  refs.devicePill.textContent = selected ? `${selected.device_id}: ${selected.profile_id}` : 'No device';
  refs.devicePill.className = `pi-control-device-pill ${selected && selected.profile_id ? selected.profile_id.replace(/[^a-z0-9_-]/gi, '-') : ''}`;
}

function renderViewPicker(refs, state) {
  const activeProfile = activeProfileForSelectedDevice(state);
  refs.viewButtons.forEach((button) => {
    const profileId = button.dataset.profileId;
    button.classList.toggle('selected', state.selectedView === profileId);
    button.classList.toggle('device-active', activeProfile === profileId);
  });
}

function filteredOutputs(state) {
  const q = String(state.filter || '').trim().toLowerCase();
  if (!q) return state.outputs;
  return state.outputs.filter((output) => outputSearchText(output).includes(q));
}

function renderPreview(refs, state, payload) {
  refs.preview.innerHTML = '';
  if (!payload) {
    refs.preview.innerHTML = '<div class="pi-control-empty">Focus an output to preview it.</div>';
    return;
  }
  const ref = payload.ref || {};
  const header = refs.root.ownerDocument.createElement('div');
  header.className = 'pi-control-preview-header';
  header.textContent = `${ref.title || ref.output_id || 'Agent output'} · ${ref.content_type || 'unknown'}`;
  refs.preview.appendChild(header);
  refs.preview.appendChild(renderAsset(refs.root.ownerDocument, ref.content_type, payload.payload, { classPrefix: 'pi-control' }));
}

async function loadPreview(refs, state, runtime, outputId) {
  state.selectedOutputId = outputId;
  const output = state.outputs.find((item) => item.output_id === outputId);
  if (output) {
    refs.preview.innerHTML = `<div class="pi-control-empty">Loading ${escapeHtml(output.title || outputId)}...</div>`;
  }
  try {
    const payload = await hubFetch(runtime, `/outputs/${encodeURIComponent(outputId)}`, {});
    if (state.selectedOutputId === outputId) renderPreview(refs, state, payload);
  } catch (err) {
    if (state.selectedOutputId === outputId) {
      refs.preview.innerHTML = `<div class="pi-control-error">Preview failed: ${escapeHtml(err.message || err)}</div>`;
    }
  }
}

function renderOutputRows(refs, state, runtime) {
  const outputs = filteredOutputs(state);
  refs.outputCount.textContent = `${outputs.length} output${outputs.length === 1 ? '' : 's'}`;
  refs.clearAllBtn.disabled = outputs.length === 0;
  const viewportHeight = refs.outputScroller.clientHeight || 420;
  const visibleCount = Math.max(1, Math.ceil(viewportHeight / VIRTUAL_ROW_HEIGHT) + VIRTUAL_OVERSCAN * 2);
  const start = Math.max(0, Math.floor((refs.outputScroller.scrollTop || 0) / VIRTUAL_ROW_HEIGHT) - VIRTUAL_OVERSCAN);
  const end = Math.min(outputs.length, start + visibleCount);
  refs.outputSpacer.style.height = `${outputs.length * VIRTUAL_ROW_HEIGHT}px`;
  refs.outputRows.innerHTML = '';
  refs.outputRows.style.transform = `translateY(${start * VIRTUAL_ROW_HEIGHT}px)`;

  for (let i = start; i < end; i++) {
    const output = outputs[i];
    const row = refs.root.ownerDocument.createElement('div');
    row.className = `pi-control-output-row ${state.selectedOutputId === output.output_id ? 'selected' : ''}`;
    row.dataset.outputId = output.output_id;
    row.tabIndex = 0;
    row.style.height = `${VIRTUAL_ROW_HEIGHT}px`;
    row.innerHTML = `
      <span class="pi-control-output-title">${escapeHtml(output.title || output.output_id)}</span>
      <span>${escapeHtml(output.agent_id || '--')}</span>
      <span>${escapeHtml(output.content_type || '--')}</span>
      <span>${escapeHtml(fmtDate(output.created_at))}</span>
      <span class="pi-control-row-actions">
        <button type="button" data-action="show-output">Show on Pi</button>
        <button type="button" data-action="clear-output">Clear</button>
      </span>`;
    row.addEventListener('focus', () => {
      loadPreview(refs, state, runtime, output.output_id);
      renderOutputRows(refs, state, runtime);
    });
    row.addEventListener('click', (event) => {
      if (event.target && event.target.closest('button')) return;
      row.focus();
    });
    row.querySelector('[data-action="show-output"]').addEventListener('click', () => showAgentOutput(refs, state, runtime, output.output_id));
    row.querySelector('[data-action="clear-output"]').addEventListener('click', () => clearOutput(refs, state, runtime, output.output_id));
    refs.outputRows.appendChild(row);
  }

  if (!outputs.length) {
    refs.outputRows.innerHTML = '<div class="pi-control-empty inline">No outputs match the current filter.</div>';
  }
}

function renderForeclosurePanel(refs, state, runtime) {
  refs.panelTitle.textContent = 'Foreclosure Live';
  refs.panelBody.innerHTML = '';
  const section = refs.root.ownerDocument.createElement('section');
  section.className = 'pi-control-action-card';
  const show = refs.root.ownerDocument.createElement('button');
  show.type = 'button';
  show.className = 'pi-control-primary';
  show.textContent = 'Show on Pi';
  show.addEventListener('click', () => showForeclosure(refs, state, runtime));

  const label = refs.root.ownerDocument.createElement('p');
  label.className = 'pi-control-disambiguation';
  label.textContent = DISAMBIGUATION_LABEL;

  const link = refs.root.ownerDocument.createElement('a');
  link.href = `#${FORECLOSURE_DASHBOARD_ID}`;
  link.textContent = 'Open foreclosure dashboard';
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const win = refs.root.ownerDocument && refs.root.ownerDocument.defaultView ? refs.root.ownerDocument.defaultView : root;
    if (win && typeof win.selectDashboard === 'function') {
      win.selectDashboard(FORECLOSURE_DASHBOARD_ID);
    } else if (win && win.location) {
      win.location.hash = FORECLOSURE_DASHBOARD_ID;
    } else {
      link.setAttribute('data-selected-dashboard', FORECLOSURE_DASHBOARD_ID);
    }
  });

  section.append(show, label, link);
  refs.panelBody.appendChild(section);
}

function renderAgentOutputPanel(refs, state, runtime) {
  refs.panelTitle.textContent = 'Agent Output';
  refs.panelBody.innerHTML = `
    <section class="pi-control-output-panel">
      <div class="pi-control-output-toolbar">
        <input type="search" data-role="output-filter" placeholder="Filter title, agent, tags" value="${escapeHtml(state.filter)}">
        <span data-role="output-count"></span>
        <button type="button" data-role="clear-all">Clear all</button>
      </div>
      <div class="pi-control-output-grid-head">
        <span>title</span><span>agent_id</span><span>content_type</span><span>created_at</span><span>actions</span>
      </div>
      <div class="pi-control-output-scroller" data-role="output-scroller">
        <div class="pi-control-output-spacer" data-role="output-spacer"></div>
        <div class="pi-control-output-rows" data-role="output-rows"></div>
      </div>
      <div class="pi-control-preview" data-role="preview"></div>
    </section>`;
  refs.filterInput = refs.panelBody.querySelector('[data-role="output-filter"]');
  refs.outputCount = refs.panelBody.querySelector('[data-role="output-count"]');
  refs.clearAllBtn = refs.panelBody.querySelector('[data-role="clear-all"]');
  refs.outputScroller = refs.panelBody.querySelector('[data-role="output-scroller"]');
  refs.outputSpacer = refs.panelBody.querySelector('[data-role="output-spacer"]');
  refs.outputRows = refs.panelBody.querySelector('[data-role="output-rows"]');
  refs.preview = refs.panelBody.querySelector('[data-role="preview"]');
  refs.filterInput.addEventListener('input', () => {
    state.filter = refs.filterInput.value || '';
    refs.outputScroller.scrollTop = 0;
    renderOutputRows(refs, state, runtime);
  });
  refs.outputScroller.addEventListener('scroll', () => renderOutputRows(refs, state, runtime));
  refs.clearAllBtn.addEventListener('click', () => clearAllOutputs(refs, state, runtime));
  renderOutputRows(refs, state, runtime);
  renderPreview(refs, state, null);
}

function renderSpecsPanel(refs, state, runtime) {
  refs.panelTitle.textContent = 'Specs';
  refs.panelBody.innerHTML = '';
  const section = refs.root.ownerDocument.createElement('section');
  section.className = 'pi-control-action-card';
  const show = refs.root.ownerDocument.createElement('button');
  show.type = 'button';
  show.className = 'pi-control-primary';
  show.textContent = 'Show on Pi';
  show.addEventListener('click', () => showSpecs(refs, state, runtime));

  const label = refs.root.ownerDocument.createElement('p');
  label.className = 'pi-control-disambiguation';
  label.textContent = SPECS_DISAMBIGUATION_LABEL;

  const link = refs.root.ownerDocument.createElement('a');
  link.href = `#${SPECS_DASHBOARD_ID}`;
  link.textContent = 'Open specs dashboard';
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const win = refs.root.ownerDocument && refs.root.ownerDocument.defaultView ? refs.root.ownerDocument.defaultView : root;
    if (win && typeof win.selectDashboard === 'function') {
      win.selectDashboard(SPECS_DASHBOARD_ID);
    } else if (win && win.location) {
      win.location.hash = SPECS_DASHBOARD_ID;
    } else {
      link.setAttribute('data-selected-dashboard', SPECS_DASHBOARD_ID);
    }
  });

  section.append(show, label, link);
  refs.panelBody.appendChild(section);
}

function renderPanel(refs, state, runtime) {
  if (state.selectedView === AGENT_OUTPUT_PROFILE_ID) renderAgentOutputPanel(refs, state, runtime);
  else if (state.selectedView === SPECS_PROFILE_ID) renderSpecsPanel(refs, state, runtime);
  else renderForeclosurePanel(refs, state, runtime);
}

function renderAll(refs, state, runtime) {
  if (!state.selectedDeviceId && state.devices.length) state.selectedDeviceId = state.devices[0].device_id;
  if (!state.selectedView) state.selectedView = activeProfileForSelectedDevice(state) || FORECLOSURE_PROFILE_ID;
  renderDevices(refs, state);
  renderViewPicker(refs, state);
  renderPanel(refs, state, runtime);
}

async function refreshInitialState(refs, state, runtime) {
  const startedAt = runtime.now ? runtime.now() : Date.now();
  state.initialFetchStartedAt = startedAt;
  refs.statusText.textContent = 'Fetching initial state...';
  try {
    const [outputs, devices] = await Promise.all([
      hubFetch(runtime, '/outputs', {}),
      hubFetch(runtime, '/control/devices', {}),
    ]);
    state.outputs = normalizeOutputs(outputs);
    state.devices = Array.isArray(devices) ? devices.slice() : [];
    if (!state.devices.some((d) => d.device_id === state.selectedDeviceId)) {
      state.selectedDeviceId = state.devices[0] ? state.devices[0].device_id : '';
    }
    if (state.selectedDeviceId) {
      const selected = currentDevice(state);
      if (selected && selected.profile_id) state.selectedView = selected.profile_id;
    }
    refs.statusText.textContent = 'Ready';
    renderAll(refs, state, runtime);
  } catch (err) {
    refs.statusText.textContent = err.message || String(err);
    refs.panelBody.innerHTML = `<div class="pi-control-error">Initial state failed: ${escapeHtml(err.message || err)}</div>`;
  }
}

async function showForeclosure(refs, state, runtime) {
  const device = currentDevice(state);
  if (!device) return;
  await hubFetch(runtime, '/control/set-active-profile', {
    method: 'POST',
    write: true,
    body: { device_id: device.device_id, profile_id: FORECLOSURE_PROFILE_ID, profile_state: {} },
  });
}

async function showSpecs(refs, state, runtime) {
  const device = currentDevice(state);
  if (!device) return;
  await hubFetch(runtime, '/control/set-active-profile', {
    method: 'POST',
    write: true,
    body: { device_id: device.device_id, profile_id: SPECS_PROFILE_ID, profile_state: {} },
  });
}

async function showAgentOutput(refs, state, runtime, outputId) {
  const device = currentDevice(state);
  if (!device || !outputId) return;
  await hubFetch(runtime, '/control/set-active-profile', {
    method: 'POST',
    write: true,
    body: {
      device_id: device.device_id,
      profile_id: AGENT_OUTPUT_PROFILE_ID,
      profile_state: { selected_output_id: outputId },
    },
  });
}

async function clearOutput(refs, state, runtime, outputId) {
  if (!outputId) return;
  if (!(await runtime.confirmAction(`Clear output ${outputId}?`))) return;
  await hubFetch(runtime, '/outputs/clear', {
    method: 'POST',
    write: true,
    body: { output_id: outputId },
  });
  await refreshInitialState(refs, state, runtime);
}

async function clearAllOutputs(refs, state, runtime) {
  if (!(await runtime.confirmAction('Clear all agent outputs?'))) return;
  await hubFetch(runtime, '/outputs/clear-all', { method: 'POST', write: true, body: {} });
  await refreshInitialState(refs, state, runtime);
}

function applyOutputsChanged(refs, state, runtime, event) {
  const removed = new Set(Array.isArray(event.removed) ? event.removed.map(String) : []);
  let outputs = state.outputs.filter((output) => !removed.has(String(output.output_id)));
  const byId = new Map(outputs.map((output) => [String(output.output_id), output]));
  for (const added of Array.isArray(event.added) ? event.added : []) {
    if (added && added.output_id) byId.set(String(added.output_id), added);
  }
  state.outputs = sortOutputs(Array.from(byId.values()));
  if (state.selectedView === AGENT_OUTPUT_PROFILE_ID) renderPanel(refs, state, runtime);
}

function applyDeviceStateChanged(refs, state, runtime, event) {
  if (!event.device_id) return;
  const existing = state.devices.find((device) => device.device_id === event.device_id);
  if (existing && existing.updated_at && event.updated_at && String(existing.updated_at) === String(event.updated_at)) {
    return;
  }
  const next = {
    ...(existing || {}),
    device_id: event.device_id,
    profile_id: event.profile_id,
    profile_state: event.profile_state || {},
    updated_at: event.updated_at || event.emitted_at || new Date().toISOString(),
    connected: existing ? existing.connected : true,
  };
  if (existing) Object.assign(existing, next);
  else state.devices.push(next);
  if (!state.selectedDeviceId) state.selectedDeviceId = next.device_id;
  if (state.selectedDeviceId === next.device_id) state.selectedView = next.profile_id || state.selectedView;
  renderAll(refs, state, runtime);
}

function handleControlEvent(refs, state, runtime, event) {
  if (!event || isEventBefore(event.emitted_at, state.initialFetchStartedAt)) return;
  if (event.type === 'outputs.changed') applyOutputsChanged(refs, state, runtime, event);
  if (event.type === 'device_state.changed') applyDeviceStateChanged(refs, state, runtime, event);
}

function reconnectDelayMs(state) {
  const base = Math.min(MAX_RECONNECT_SECONDS, [1, 2, 5, 10, 30][Math.min(state.reconnectAttempt, 4)]);
  state.reconnectAttempt += 1;
  return Math.min(MAX_RECONNECT_SECONDS * 1000, Math.round((base + Math.random() * base * 0.25) * 1000));
}

function closeEventSource(state) {
  if (!state.eventSource) return;
  try { state.eventSource.close(); } catch (_) {}
  state.eventSource = null;
}

function connectControlStream(refs, state, runtime) {
  if (!state.mounted) return;
  if (!runtime.EventSource || !runtime.readToken || !runtime.hubUrl) {
    updateLiveState(refs, state, false, 'SSE unavailable');
    return;
  }
  closeEventSource(state);
  let url;
  try {
    url = new URL(`/stream/${CONTROL_ENVELOPE_ID}`, runtime.hubUrl);
    url.searchParams.set('token', runtime.readToken);
  } catch (err) {
    refs.statusText.textContent = `Invalid hub URL: ${err.message}`;
    updateLiveState(refs, state, false, 'bad hub URL');
    return;
  }
  const source = new runtime.EventSource(url.toString());
  state.eventSource = source;
  source.onopen = () => {
    state.reconnectAttempt = 0;
    updateLiveState(refs, state, true);
  };
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      handleControlEvent(refs, state, runtime, event);
    } catch (_) {}
  };
  source.onerror = () => {
    if (!state.mounted) return;
    closeEventSource(state);
    updateLiveState(refs, state, false, 'reconnecting');
    const delay = reconnectDelayMs(state);
    if (state.reconnectTimer) runtime.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = runtime.setTimeout(() => {
      state.reconnectTimer = null;
      if (!state.mounted) return;
      refreshInitialState(refs, state, runtime);
      connectControlStream(refs, state, runtime);
    }, delay);
  };
}

function mount(container) {
  const doc = container.ownerDocument;
  const win = doc && doc.defaultView ? doc.defaultView : root;
  const runtime = loadRuntime(win);
  container.innerHTML = '';
  const state = {
    mounted: true,
    outputs: [],
    devices: [],
    selectedDeviceId: '',
    selectedView: FORECLOSURE_PROFILE_ID,
    selectedOutputId: '',
    filter: '',
    eventSource: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    connected: false,
    initialFetchStartedAt: 0,
  };

  const rootEl = doc.createElement('div');
  rootEl.className = 'pi-control-dashboard';
  rootEl.innerHTML = `
    <style>
      .pi-control-dashboard { height:100%; min-height:0; display:flex; flex-direction:column; gap:14px; padding:16px; color:#dce8e1; background:#0b1110; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; }
      .pi-control-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
      .pi-control-head h2 { margin:0 0 4px; font-size:20px; color:#f1faf5; }
      .pi-control-head p { margin:0; color:#8fa49a; font-size:13px; }
      .pi-control-live { display:flex; align-items:center; gap:8px; color:#90a49a; font-size:12px; white-space:nowrap; }
      .pi-control-live-dot { width:9px; height:9px; border-radius:50%; background:#f47067; box-shadow:0 0 0 3px rgba(244,112,103,0.12); }
      .pi-control-live-dot.live { background:#56d364; box-shadow:0 0 0 3px rgba(86,211,100,0.13); }
      .pi-control-device-row { display:flex; align-items:center; gap:12px; padding:10px 12px; background:#121b18; border:1px solid #21332b; border-radius:8px; }
      .pi-control-device-row select, .pi-control-output-toolbar input { background:#0c1411; color:#e6f2eb; border:1px solid #2b4438; border-radius:6px; padding:8px 10px; }
      .pi-control-device-meta { color:#899d94; font-size:12px; }
      .pi-control-device-pill { margin-left:auto; padding:5px 9px; border-radius:999px; border:1px solid #2b4438; color:#b9c9c0; background:#0c1411; font-size:12px; }
      .pi-control-device-pill.agent-output { border-color:#2dd4bf66; color:#8ee4bf; }
      .pi-control-device-pill.foreclosure-live { border-color:#56d36466; color:#9ce6b6; }
      .pi-control-device-pill.specs { border-color:#f5b78a66; color:#f5b78a; }
      .pi-control-main { flex:1; min-height:0; display:grid; grid-template-columns:190px minmax(0,1fr); gap:14px; }
      .pi-control-views { display:flex; flex-direction:column; gap:8px; min-height:0; }
      .pi-control-view-btn { text-align:left; border:1px solid #22332d; background:#111a16; color:#dce8e1; border-radius:8px; padding:12px; cursor:pointer; }
      .pi-control-view-btn.selected { outline:1px solid #79c0ff; }
      .pi-control-view-btn.device-active { border-color:#56d364; background:#15251d; }
      .pi-control-view-btn span { display:block; color:#81978c; font-size:11px; margin-top:4px; }
      .pi-control-panel { min-height:0; display:flex; flex-direction:column; border:1px solid #22332d; border-radius:8px; background:#101815; overflow:hidden; }
      .pi-control-panel-head { display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border-bottom:1px solid #22332d; }
      .pi-control-panel-head h3 { margin:0; font-size:16px; }
      .pi-control-status { color:#8fa49a; font-size:12px; }
      .pi-control-panel-body { flex:1; min-height:0; padding:14px; overflow:hidden; }
      .pi-control-action-card { display:flex; flex-direction:column; align-items:flex-start; gap:14px; max-width:560px; }
      .pi-control-primary, .pi-control-output-row button, .pi-control-output-toolbar button { border:1px solid #2d4a3b; background:#173126; color:#dff7ea; border-radius:6px; padding:8px 11px; cursor:pointer; }
      .pi-control-primary { background:#1d5f40; border-color:#2e8f5f; font-weight:700; }
      .pi-control-disambiguation { margin:0; color:#f4bf4f; }
      .pi-control-action-card a { color:#79c0ff; text-decoration:none; }
      .pi-control-output-panel { height:100%; min-height:0; display:grid; grid-template-rows:auto auto minmax(180px,1fr) minmax(160px,0.7fr); gap:10px; }
      .pi-control-output-toolbar { display:flex; align-items:center; gap:10px; }
      .pi-control-output-toolbar input { min-width:260px; }
      .pi-control-output-toolbar span { color:#8fa49a; font-size:12px; }
      .pi-control-output-toolbar button { margin-left:auto; }
      .pi-control-output-grid-head, .pi-control-output-row { display:grid; grid-template-columns:minmax(180px,2fr) minmax(100px,1fr) 110px 132px 156px; gap:10px; align-items:center; }
      .pi-control-output-grid-head { color:#83978d; font-size:11px; text-transform:uppercase; letter-spacing:0; padding:0 10px; }
      .pi-control-output-scroller { position:relative; min-height:0; overflow:auto; border:1px solid #22332d; border-radius:8px; background:#0c1411; }
      .pi-control-output-spacer { width:1px; }
      .pi-control-output-rows { position:absolute; inset:0 0 auto 0; }
      .pi-control-output-row { padding:0 10px; border-bottom:1px solid #1b2a24; color:#dbe7e0; font-size:12px; cursor:pointer; }
      .pi-control-output-row:hover, .pi-control-output-row.selected { background:#14231c; }
      .pi-control-output-title { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; color:#f1faf5; }
      .pi-control-row-actions { display:flex; gap:6px; }
      .pi-control-row-actions button { padding:5px 7px; font-size:11px; }
      .pi-control-preview { min-height:0; overflow:auto; border:1px solid #22332d; border-radius:8px; background:#0c1411; padding:12px; }
      .pi-control-preview-header { color:#8ee4bf; margin-bottom:10px; font-weight:700; }
      .pi-control-markdown-preview { color:#e6f2eb; line-height:1.45; }
      .pi-control-markdown-preview h1, .pi-control-markdown-preview h2, .pi-control-markdown-preview h3 { color:#56d364; margin:0.3em 0; }
      .pi-control-markdown-preview code { color:#fff8c5; background:rgba(244,191,79,0.12); border-radius:4px; padding:1px 4px; }
      .pi-control-markdown-preview pre { background:#080f0c; padding:10px; border-radius:6px; overflow:auto; }
      .pi-control-preview-table-wrap { overflow:auto; }
      .pi-control-preview table { width:100%; border-collapse:collapse; font-size:12px; }
      .pi-control-preview th, .pi-control-preview td { padding:7px 8px; border-bottom:1px solid #1f3028; text-align:left; }
      .pi-control-preview th { color:#56d364; text-transform:uppercase; font-size:11px; }
      .pi-control-preview td.numeric { text-align:right; color:#fff8c5; }
      .pi-control-empty, .pi-control-error { color:#879b91; padding:18px; border:1px dashed #273a32; border-radius:8px; }
      .pi-control-error { color:#f47067; border-color:#5c2927; }
      .pi-control-empty.inline { margin:12px; }
    </style>
    <div class="pi-control-head">
      <div>
        <h2>Pi Control</h2>
        <p>Switch what each tailnet wall display is showing.</p>
      </div>
      <div>
        <div class="pi-control-live"><span data-role="live-dot" class="pi-control-live-dot offline"></span><span data-role="live-text">connecting</span></div>
        <div data-role="staleness"></div>
      </div>
    </div>
    <div class="pi-control-device-row">
      <select data-role="device-select" aria-label="Pi device"></select>
      <span data-role="device-meta" class="pi-control-device-meta"></span>
      <span data-role="device-pill" class="pi-control-device-pill"></span>
    </div>
    <div class="pi-control-main">
      <nav class="pi-control-views">
        <button type="button" class="pi-control-view-btn" data-profile-id="foreclosure-live">Foreclosure Live<span>Display foreclosure wall profile</span></button>
        <button type="button" class="pi-control-view-btn" data-profile-id="agent-output">Agent Output<span>Display selected agent output</span></button>
        <button type="button" class="pi-control-view-btn" data-profile-id="specs">Specs<span>Display specs kanban (work/ tree)</span></button>
      </nav>
      <section class="pi-control-panel">
        <div class="pi-control-panel-head">
          <h3 data-role="panel-title"></h3>
          <span data-role="status-text" class="pi-control-status">Starting...</span>
        </div>
        <div data-role="panel-body" class="pi-control-panel-body"></div>
      </section>
    </div>`;
  container.appendChild(rootEl);

  const refs = {
    root: rootEl,
    runtime,
    state,
    liveDot: rootEl.querySelector('[data-role="live-dot"]'),
    liveText: rootEl.querySelector('[data-role="live-text"]'),
    staleness: rootEl.querySelector('[data-role="staleness"]'),
    deviceSelect: rootEl.querySelector('[data-role="device-select"]'),
    deviceMeta: rootEl.querySelector('[data-role="device-meta"]'),
    devicePill: rootEl.querySelector('[data-role="device-pill"]'),
    viewButtons: Array.from(rootEl.querySelectorAll('.pi-control-view-btn')),
    panelTitle: rootEl.querySelector('[data-role="panel-title"]'),
    statusText: rootEl.querySelector('[data-role="status-text"]'),
    panelBody: rootEl.querySelector('[data-role="panel-body"]'),
  };

  refs.deviceSelect.addEventListener('change', () => {
    state.selectedDeviceId = refs.deviceSelect.value;
    const selected = currentDevice(state);
    if (selected && selected.profile_id) state.selectedView = selected.profile_id;
    renderAll(refs, state, runtime);
  });
  refs.viewButtons.forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedView = button.dataset.profileId;
      renderAll(refs, state, runtime);
    });
  });

  renderAll(refs, state, runtime);
  refreshInitialState(refs, state, runtime);
  connectControlStream(refs, state, runtime);
  return refs;
}

function unmount(refs) {
  if (!refs || !refs.state) return;
  refs.state.mounted = false;
  closeEventSource(refs.state);
  if (refs.state.reconnectTimer) {
    refs.runtime.clearTimeout(refs.state.reconnectTimer);
    refs.state.reconnectTimer = null;
  }
}

const dashboard = {
  id: 'pi-control',
  name: 'Pi Control',
  description: 'Switch tailnet Pi displays and agent-output panels',
  color: '#2dd4bf',
  mount,
  unmount,
};

if (root && root.DASHBOARDS) root.DASHBOARDS.push(dashboard);

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    dashboard,
    mount,
    unmount,
    _test: {
      DISAMBIGUATION_LABEL,
      SPECS_DISAMBIGUATION_LABEL,
      FORECLOSURE_DASHBOARD_ID,
      SPECS_DASHBOARD_ID,
      SPECS_PROFILE_ID,
      MAX_RECONNECT_SECONDS,
      maskTokenInUrl,
      renderMarkdown,
      normalizeOutputs,
      handleControlEvent,
      reconnectDelayMs,
    },
  };
}

})(typeof window !== 'undefined' ? window : null);
