// dashboard-library — shared dashboard definition layer.
//
// ONE canonical board definition renders on BOTH the Pi wall display
// (control-dashboard-display) and the Pentacle desktop (pentacle/renderer).
// Platform is a render flag (`mode: 'display' | 'interactive'`), not a fork.
//
// Module format: a single CLASSIC UMD global file (no ESM). Loaded via
// `<script src>` on both surfaces (exposes `globalThis.PublicDashboardLibrary`),
// and `require()`-able in node tests (CommonJS branch). This is mandatory:
// Pentacle's renderer runs over file:// with nodeIntegration and Chromium
// blocks `import` over file:// (origin null). See
// Consumers inject this optional dependency explicitly.
//
// 43060d270344 is replaced by build.js with the source git short-sha.

(function (root, factory) {
  const api = factory();
  root.PublicDashboardLibrary = api;
  root.TriforceDashboards = api; // Existing opt-in Pentacle adapter compatibility.
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const VERSION = '43060d270344';

  // ── Status set ────────────────────────────────────────────────
  // Canonical JS mirror of triforce-shared/specs_parser DEFAULT_STATUSES and
  // work/statuses.json (post active→status migration). The real source of
  // truth is the daemon/hub `statuses` payload; this is the fallback.
  const DEFAULT_STATUSES = Object.freeze([
    Object.freeze({ name: 'backlog',       order: 1, display_label: 'Backlog',       color: '#8aa097', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'analysis',      order: 2, display_label: 'Analysis',      color: '#a890ff', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'ready_for_dev', order: 3, display_label: 'Ready for Dev', color: '#a8b8ff', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'in_progress',   order: 4, display_label: 'In Progress',   color: '#56d364', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'needs_qa',      order: 5, display_label: 'Needs QA',      color: '#f5b78a', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'blocked',       order: 6, display_label: 'Blocked',       color: '#d4a300', is_terminal: false, default_visible: true  }),
    Object.freeze({ name: 'completed',     order: 7, display_label: 'Completed',     color: '#2dd4bf', is_terminal: true,  default_visible: false }),
    Object.freeze({ name: 'deprecated',    order: 8, display_label: 'Deprecated',    color: '#7a7a7a', is_terminal: true,  default_visible: false }),
  ]);

  function statusesOrDefault(statuses) {
    if (Array.isArray(statuses) && statuses.length > 0) return statuses;
    return DEFAULT_STATUSES;
  }

  // Filter statuses by `default_visible`. showAll=true returns all (incl.
  // terminal). Missing `default_visible` treated as visible (v1 back-compat).
  function selectVisibleStatuses(statuses, showAll) {
    const list = statusesOrDefault(statuses);
    if (showAll) return list;
    return list.filter((s) => s.default_visible !== false);
  }

  // Partition rows into kanban columns. CANONICAL: keys off `row.status` only
  // (the legacy `row.status || row.lifecycle` alias the Pi still carries was
  // dropped on the Pentacle side; the shared helper adopts the newer form and
  // corrects the Pi). status_unknown / unconfigured folders → "Other" bucket;
  // synthetic rows → `unresolved`.
  function partitionRows(rows, statuses) {
    const unresolved = [];
    const byStatus = {};
    const otherStatusRows = [];
    for (const s of statusesOrDefault(statuses)) byStatus[s.name] = [];
    for (const row of rows || []) {
      if (!row || typeof row !== 'object') continue;
      if (row.synthetic === true) { unresolved.push(row); continue; }
      const name = row.status;
      if (!name) continue;
      if (row.status_unknown === true || !(name in byStatus)) {
        otherStatusRows.push(row);
      } else {
        byStatus[name].push(row);
      }
    }
    return { unresolved, byStatus, otherStatusRows };
  }

  // Stable list sort shared by Pi + desktop: next_action-presence, then id.
  function sortForList(rows) {
    return (rows || []).slice().sort((a, b) => {
      const an = a && a.next_action ? 1 : 0;
      const bn = b && b.next_action ? 1 : 0;
      if (an !== bn) return bn - an;
      const ai = String((a && (a.spec_id || a.id)) || '');
      const bi = String((b && (b.spec_id || b.id)) || '');
      return ai.localeCompare(bi);
    });
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ── Design tokens (JS mirror of tokens.css custom properties) ──
  const TOKENS = Object.freeze({
    shellBg: '#0c1310',
    cardBg: '#121d18',
    cardBorder: '#1f2e27',
    textPrimary: '#e6efe9',
    textMuted: '#8aa097',
    accent: '#56d364',
  });

  // Inject a board's CSS into document.head exactly once (idempotent). Lets a
  // board definition be fully self-contained — the same markup styles
  // identically on both surfaces with no per-surface stylesheet wiring. Guarded
  // for non-DOM contexts (node string-render tests) where document is undefined.
  function injectBoardCss(id, css) {
    if (typeof document === 'undefined' || !document.head) return;
    const marker = `td-css-${id}`;
    if (document.getElementById(marker)) return;
    const style = document.createElement('style');
    style.id = marker;
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ── Manifest-driven visibility ────────────────────────────────
  // A board's manifest declares its addressable elements. Visibility is
  // derived from the manifest + mode — boards MUST NOT branch on `mode`
  // directly; they call ctx.isVisible(id). Precedence (most→least specific):
  //   1. visibleIn array present  → visible iff mode ∈ visibleIn  (WINS over interactiveOnly)
  //   2. interactiveOnly === true → visible only in 'interactive' mode
  //   3. neither                  → always visible
  // A manifest should set at most one of visibleIn / interactiveOnly; if both
  // are set, visibleIn is authoritative (it is the explicit general form).
  function elementVisibleInMode(element, mode) {
    if (!element) return false;
    if (Array.isArray(element.visibleIn)) return element.visibleIn.indexOf(mode) !== -1;
    if (element.interactiveOnly === true) return mode === 'interactive';
    return true;
  }

  function manifestElement(manifest, id) {
    const els = (manifest && manifest.elements) || [];
    for (const e of els) if (e && e.id === id) return e;
    return null;
  }

  function visibleElements(manifest, mode) {
    const els = (manifest && manifest.elements) || [];
    return els.filter((e) => elementVisibleInMode(e, mode));
  }

  const MODES = Object.freeze(['display', 'interactive']);

  // ── Board definition contract ─────────────────────────────────
  // A board definition: { id, name, manifest, render(container, state, ctx) }.
  // ctx = { mode, helpers, tokens, isVisible(elementId), escapeHtml }.
  function defineBoard(def) {
    if (!def || typeof def !== 'object') throw new Error('board definition must be an object');
    if (!def.id) throw new Error('board definition requires an id');
    const stateless = typeof def.render === 'function';
    const stateful = typeof def.mount === 'function';
    if (!stateless && !stateful) throw new Error(`board "${def.id}" requires render() (stateless) or mount() (stateful)`);
    if (!def.manifest || !Array.isArray(def.manifest.elements)) {
      throw new Error(`board "${def.id}" requires manifest.elements[]`);
    }
    return def;
  }

  // `options` = { mode, actions }. `actions` is an optional bag of interaction
  // callbacks (e.g. setBatchGate/refetch/refreshNow) the adapter injects so a
  // stateful board never references platform globals (window.cc) directly: the
  // desktop adapter binds them to chat_streamd IPC, the Pi leaves them unset
  // (and the board's interactiveOnly controls are hidden anyway). Backward
  // compatible — `mode` may still be passed as a bare string.
  function makeCtx(def, options) {
    const opts = (typeof options === 'string') ? { mode: options } : (options || {});
    const m = opts.mode || 'display';
    if (MODES.indexOf(m) === -1) throw new Error(`unknown mode "${m}"`);
    return {
      mode: m,
      actions: opts.actions || {},
      helpers: { DEFAULT_STATUSES, statusesOrDefault, selectVisibleStatuses, partitionRows, sortForList, escapeHtml },
      tokens: TOKENS,
      escapeHtml,
      isVisible: (elementId) => elementVisibleInMode(manifestElement(def.manifest, elementId), m),
    };
  }

  // Stateless single-shot render (specs/demo adapters use this). For stateful
  // boards use mountBoard/updateBoard/unmountBoard below.
  function renderBoard(container, def, state, options) {
    const ctx = makeCtx(def, options);
    return def.render(container, state, ctx);
  }

  // Stateful lifecycle: mountBoard returns refs (the board's own handle, tagged
  // with __ctx/__container so update/unmount work without re-deriving ctx).
  // Falls back to stateless render so adapters can treat all boards uniformly.
  function mountBoard(container, def, state, options) {
    const ctx = makeCtx(def, options);
    if (typeof def.mount === 'function') {
      const refs = def.mount(container, ctx) || {};
      refs.__ctx = ctx;
      refs.__container = container;
      if (state !== undefined && typeof def.update === 'function') def.update(refs, state, ctx);
      return refs;
    }
    def.render(container, state, ctx);
    return { __ctx: ctx, __container: container };
  }
  function updateBoard(refs, def, state) {
    if (!refs) return undefined;
    const ctx = refs.__ctx || makeCtx(def, 'display');
    if (typeof def.update === 'function') return def.update(refs, state, ctx);
    if (typeof def.render === 'function') return def.render(refs.__container, state, ctx);
    return undefined;
  }
  function unmountBoard(refs, def) {
    if (!refs) return;
    if (def && typeof def.unmount === 'function') def.unmount(refs);
    else if (refs.__container) refs.__container.innerHTML = '';
  }

  // ── Boards ────────────────────────────────────────────────────
  const boards = {};
  function registerBoard(def) { boards[def.id] = defineBoard(def); return boards[def.id]; }
  // Specs board — the canonical cross-platform spec kanban. Renders identically
  // on the Pi (display) and the Pentacle desktop (interactive); the only
  // platform difference is the manifest `spec-drive` action (click a card to
  // open the Drive/Spawn modal) which is interactiveOnly — the desktop adapter
  // wires it via chat_streamd; the Pi shows the same cards, non-interactive.
  // Class-based markup reuses the Pi profile.css class names (.col/.card/.chip)
  // so both surfaces style consistently from one vendored stylesheet.
  function _specCardHtml(row, esc) {
    const title = esc(row.title || row.spec_id || row.id || '(untitled)');
    const next = row.next_action ? `<div class="card-next" title="${esc(row.next_action)}">${esc(row.next_action)}</div>` : '';
    const repo = row.repo ? `<span class="card-repo">${esc(row.repo)}</span>` : '';
    const statusPill = row.status ? `<span class="chip status">${esc(row.status)}</span>` : '';
    const drift = row.frontmatter_drift ? `<span class="chip drift" title="frontmatter status != folder">drift</span>` : '';
    const unknown = row.status_unknown ? `<span class="chip unknown" title="folder not in statuses.json">unknown</span>` : '';
    const leaders = (row.live_leaders && row.live_leaders.length) ? `<span class="chip leaders" title="live leaders">●${row.live_leaders.length}</span>` : '';
    const sid = esc(row.spec_id || row.id || '');
    return `<div class="card spec-row" data-spec-id="${sid}"><div class="card-meta">${statusPill}${repo}${drift}${unknown}${leaders}</div><div class="card-title">${title}</div>${next}</div>`;
  }

  function _epicKey(row) {
    const epic = row && row.epic;
    if (epic === null || epic === undefined) return '';
    return String(epic).trim();
  }

  function _epicTitle(row, key) {
    return row.epic_title || row.epicTitle || row.epic_name || row.epicName || key;
  }

  function _groupSpecsForLane(rows) {
    const groups = new Map();
    for (const row of rows) {
      const key = _epicKey(row);
      if (!key) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }

    const seen = new Set();
    const items = [];
    for (const row of rows) {
      const key = _epicKey(row);
      const group = key ? groups.get(key) : null;
      if (!group || group.length < 2) {
        items.push({ type: 'spec', row });
        continue;
      }
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ type: 'epic_group', key, title: _epicTitle(group[0], key), rows: group });
    }
    return items;
  }

  function _epicGroupCardHtml(item, esc) {
    const title = esc(item.title || item.key || '(epic)');
    const count = item.rows.length;
    const titles = item.rows.map((row) => `<li>${esc(row.title || row.spec_id || row.id || '(untitled)')}</li>`).join('');
    return `<div class="card epic-group-row" data-epic-id="${esc(item.key || '')}"><div class="card-meta"><span class="chip epic">epic</span><span class="chip">${count} specs</span></div><div class="card-title">${title}</div><ul class="epic-spec-list">${titles}</ul></div>`;
  }

  function _specLaneCardsHtml(rows, esc) {
    return _groupSpecsForLane(rows).map((item) => (
      item.type === 'epic_group' ? _epicGroupCardHtml(item, esc) : _specCardHtml(item.row, esc)
    )).join('');
  }

  function _epicCardHtml(epic, esc) {
    const title = esc(epic.title || epic.id || '(epic)');
    const status = epic.status ? `<span class="chip status">${esc(epic.status)}</span>` : '';
    const members = `<span class="chip">${(epic.members || []).length} specs</span>`;
    return `<div class="card epic-row" data-epic-id="${esc(epic.id || '')}"><div class="card-meta">${status}${members}</div><div class="card-title">${title}</div></div>`;
  }

  // Verbatim port of the OG Pi specs profile.css (the look the operator wants):
  // bluish gradient, Inter, clamp-responsive sizing for 1080p–4K, accent status
  // chips, 4px colored column tops. Scoped under .specs-shell so it's safe to
  // inject on the desktop too. .specs-shell uses 100%/100% (not 100vw/vh) so it
  // fills the Pi kiosk AND a desktop dashboard pane.
  const SPECS_CSS = `
.specs-shell{width:100%;height:100%;display:flex;flex-direction:column;gap:clamp(10px,0.9vw,22px);background:linear-gradient(180deg,#0b0f14 0%,#121923 48%,#10151c 100%);color:#f2f5f8;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:clamp(16px,1.6vw,40px);box-sizing:border-box;overflow:hidden;}
.specs-shell .specs-toggle{display:flex;gap:clamp(6px,0.5vw,12px);flex:0 0 auto;}
.specs-shell .specs-toggle button{background:rgba(255,255,255,0.06);color:#aeb8c2;border:1px solid rgba(255,255,255,0.12);border-radius:999px;padding:clamp(4px,0.4vw,10px) clamp(14px,1.1vw,26px);font-size:clamp(12px,1vw,22px);font-weight:600;cursor:pointer;font-family:inherit;}
.specs-shell .specs-toggle button.active{background:#25d0ab;color:rgba(0,0,0,0.8);border-color:#25d0ab;}
.specs-shell .kanban-row{display:flex;flex-direction:row;width:100%;gap:clamp(8px,0.7vw,18px);flex:1;min-height:0;overflow:hidden;align-items:stretch;}
.specs-shell .col{flex:1 1 0;min-width:0;min-height:0;display:flex;flex-direction:column;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.10);border-top:4px solid var(--accent,#8aa097);border-radius:10px;overflow:hidden;}
.specs-shell .col>header{display:flex;align-items:center;gap:clamp(6px,0.6vw,14px);padding:clamp(10px,0.9vw,22px) clamp(12px,1vw,22px);border-bottom:1px solid rgba(255,255,255,0.10);}
.specs-shell .col>header .dot{width:clamp(8px,0.8vw,16px);height:clamp(8px,0.8vw,16px);border-radius:50%;background:var(--accent,#8aa097);flex-shrink:0;}
.specs-shell .col>header .label{text-transform:uppercase;letter-spacing:0.08em;font-size:clamp(12px,1vw,22px);font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.specs-shell .col>header .count{font-size:clamp(13px,1vw,22px);font-weight:600;color:#aeb8c2;font-variant-numeric:tabular-nums;}
.specs-shell .cards{flex:1;overflow-y:auto;overflow-x:hidden;padding:clamp(8px,0.7vw,16px);display:flex;flex-direction:column;gap:clamp(6px,0.5vw,12px);}
.specs-shell .card{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.10);border-radius:8px;padding:clamp(8px,0.7vw,16px) clamp(10px,0.9vw,18px);}
.specs-shell[data-mode="interactive"] .card.spec-row{cursor:pointer;}
.specs-shell .card-meta{display:flex;gap:clamp(4px,0.4vw,10px);flex-wrap:wrap;margin-bottom:clamp(4px,0.4vw,10px);}
.specs-shell .card-repo{font-size:clamp(10px,0.75vw,18px);letter-spacing:0.03em;color:#aeb8c2;background:rgba(255,255,255,0.06);border-radius:999px;padding:clamp(2px,0.2vw,5px) clamp(6px,0.5vw,12px);}
.specs-shell .chip{font-size:clamp(10px,0.75vw,18px);border-radius:999px;padding:clamp(2px,0.2vw,5px) clamp(6px,0.5vw,12px);text-transform:uppercase;letter-spacing:0.05em;}
.specs-shell .chip.drift{background:#3a2614;color:#f5b78a;}
.specs-shell .chip.unknown{background:#3a2614;color:#f5b78a;}
.specs-shell .chip.status{background:var(--accent,#8aa097);color:rgba(0,0,0,0.78);font-weight:700;}
.specs-shell .chip.leaders{background:rgba(37,208,171,0.18);color:#25d0ab;}
.specs-shell .chip.epic{background:rgba(245,183,138,0.18);color:#f5b78a;}
.specs-shell .card-title{font-size:clamp(13px,1vw,22px);font-weight:600;color:#f2fbf5;line-height:1.25;}
.specs-shell .card-next{margin-top:clamp(4px,0.4vw,10px);font-size:clamp(11px,0.85vw,18px);color:#aeb8c2;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.specs-shell .epic-spec-list{margin:clamp(6px,0.55vw,12px) 0 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:clamp(4px,0.35vw,8px);}
.specs-shell .epic-spec-list li{font-size:clamp(11px,0.85vw,18px);line-height:1.25;color:#c9d4dd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.specs-shell .empty{font-size:clamp(11px,0.85vw,18px);color:#6a7e75;text-align:center;padding:clamp(12px,1vw,22px) clamp(6px,0.5vw,12px);border:1px dashed rgba(255,255,255,0.10);border-radius:8px;}
.specs-shell .epics-grid{display:flex;flex-wrap:wrap;gap:clamp(8px,0.7vw,16px);overflow:auto;align-content:flex-start;flex:1;}
.specs-shell .epic-row{flex:0 0 clamp(220px,20vw,360px);}
`;

  registerBoard({
    id: 'specs',
    name: 'Specs',
    description: 'Cross-platform spec kanban (Specs/Epics toggle); Drive action is desktop-interactive, the Pi shows the same board read-only.',
    color: '#f5b78a',
    manifest: {
      elements: [
        { id: 'specs-epics-toggle', label: 'Specs/Epics toggle' },
        { id: 'kanban', label: 'Kanban' },
        { id: 'spec-card', label: 'Spec card' },
        { id: 'spec-drive', label: 'Drive / Spawn (click a card)', interactiveOnly: true },
      ],
    },
    render(container, state, ctx) {
      injectBoardCss('specs', SPECS_CSS);
      const s = state || {};
      const view = s.view === 'epics' ? 'epics' : 'specs';
      const esc = ctx.escapeHtml;
      // Toggle (both surfaces get it — this is the Pi's missing piece).
      const toggle = ctx.isVisible('specs-epics-toggle')
        ? `<div class="specs-toggle" data-role="specs-toggle">
             <button type="button" data-view="specs" class="${view === 'specs' ? 'active' : ''}">Specs</button>
             <button type="button" data-view="epics" class="${view === 'epics' ? 'active' : ''}">Epics</button>
           </div>` : '';
      let body;
      if (view === 'epics') {
        const epics = Array.isArray(s.epics) ? s.epics : [];
        const cards = epics.length ? epics.map((e) => _epicCardHtml(e, esc)).join('') : '<div class="empty">No epics</div>';
        body = `<div class="epics-grid">${cards}</div>`;
      } else {
        const statuses = ctx.helpers.selectVisibleStatuses(s.statuses, !!s.showAll);
        const { byStatus, otherStatusRows } = ctx.helpers.partitionRows(s.specs, s.statuses);
        const cols = statuses.map((st) => {
          const rows = ctx.helpers.sortForList(byStatus[st.name] || []);
          const cards = rows.length ? _specLaneCardsHtml(rows, esc) : '<div class="empty">No specs</div>';
          return `<section class="col" data-status="${esc(st.name)}" style="--accent:${esc(st.color || '#8aa097')}"><header><span class="dot"></span><span class="label">${esc(st.display_label || st.name)}</span><span class="count">${rows.length}</span></header><div class="cards">${cards}</div></section>`;
        });
        if (otherStatusRows && otherStatusRows.length) {
          const rows = ctx.helpers.sortForList(otherStatusRows);
          cols.push(`<section class="col" data-status="__other__" style="--accent:#f5b78a"><header><span class="dot"></span><span class="label">Other</span><span class="count">${rows.length}</span></header><div class="cards">${_specLaneCardsHtml(rows, esc)}</div></section>`);
        }
        body = `<div class="kanban-row" data-role="kanban">${cols.join('')}</div>`;
      }
      const html = `<main class="specs-shell" data-mode="${ctx.mode}">${toggle}${body}</main>`;
      if (container) container.innerHTML = html;
      return html;
    },
  });

  // Verbatim port of the OG desktop foreclosure styling (pentacle styles.css
  // .pipeline-*/.stage-*/.skiptrace-* rules). var(--token) references keep their
  // desktop fallbacks AND gain dark-theme fallbacks so the Pi — which lacks the
  // desktop theme custom properties — renders correctly. Modal rules are
  // body-level (modals append to document.body) so they are intentionally
  // unscoped; the in-shell rules use the OG class names under .foreclosure-shell.
  const FORECLOSURE_CSS = `
.foreclosure-shell{width:100%;height:100%;box-sizing:border-box;overflow:auto;}
.pipeline-dashboard {
  max-width: 1200px;
  margin: 0 auto;
}

.pipeline-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 28px;
}

.pipeline-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--fg, #b5ccba);
  letter-spacing: 0.5px;
}

.pipeline-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.pipeline-updated {
  font-size: 11px;
  color: var(--fg-dim, #4d6e56);
}

.pipeline-refresh-btn {
  font-size: 10px;
  padding: 4px 10px;
}

.pipeline-loading, .pipeline-error {
  text-align: center;
  padding: 40px;
  color: var(--fg-dim, #4d6e56);
  font-size: 13px;
}

.pipeline-error { color: var(--red, #f47067); }

/* Pipeline Flow Layout */

.pipeline-stages {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0;
  padding: 20px 0;
  overflow-x: auto;
}

.pipeline-stage {
  background: var(--bg2, #121e18);
  border: 1px solid var(--border, #1e3928);
  border-radius: 10px;
  padding: 16px 20px;
  min-width: 130px;
  text-align: center;
  position: relative;
  transition: all 0.2s;
  flex-shrink: 0;
}

.pipeline-stage:hover {
  border-color: var(--stage-color, var(--border, #1e3928));
}

.pipeline-stage-active {
  border-color: var(--stage-color, var(--green, #56d364));
  box-shadow: 0 0 12px rgba(63, 185, 80, 0.15), inset 0 0 8px rgba(63, 185, 80, 0.05);
}

.pipeline-stage-name {
  font-size: 11px;
  font-weight: 700;
  color: var(--fg, #b5ccba);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}

.pipeline-stage-counts {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pipeline-count {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
}

.pipeline-count-num {
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  line-height: 1;
}

.pipeline-count.pending .pipeline-count-num {
  color: var(--yellow, #d4a72c);
}

.pipeline-count.rejected .pipeline-count-num {
  color: var(--red, #f47067);
}

.pipeline-count-label {
  font-size: 9px;
  color: var(--fg-dim, #4d6e56);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

/* Active stage pulse indicator */
.pipeline-stage-pulse {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--stage-color, var(--green, #56d364));
  animation: pipeline-pulse 1.5s ease-in-out infinite;
}

@keyframes pipeline-pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 4px var(--stage-color, var(--green, #56d364)); }
  50% { opacity: 0.4; box-shadow: 0 0 8px var(--stage-color, var(--green, #56d364)); }
}

/* Arrows between stages */
.pipeline-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 0 4px;
  opacity: 0.5;
}

/* Rejected section below pipeline */
.pipeline-rejected-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 4px;
}

.pipeline-rejected-arrow {
  display: flex;
  justify-content: center;
}

.pipeline-stage-rejected {
  border-color: rgba(244, 112, 103, 0.3);
  background: rgba(244, 112, 103, 0.05);
}

.pipeline-stage-rejected .pipeline-stage-name {
  color: var(--red, #f47067);
}

/* Details Grid */

.pipeline-details {
  margin-top: 28px;
}

.pipeline-details-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 14px;
}

.pipeline-detail-card {
  background: var(--bg2, #121e18);
  border: 1px solid var(--border, #1e3928);
  border-radius: 10px;
  padding: 14px;
}

.pipeline-detail-card-wide {
  grid-column: 1 / -1;
}

.pipeline-detail-title {
  font-size: 10px;
  font-weight: 700;
  color: var(--fg-dim, #4d6e56);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}

.pipeline-detail-content {
  font-size: 12px;
}

.pipeline-detail-scrollable {
  max-height: 200px;
  overflow-y: auto;
}

.pipeline-detail-scrollable::-webkit-scrollbar { width: 4px; }
.pipeline-detail-scrollable::-webkit-scrollbar-track { background: transparent; }
.pipeline-detail-scrollable::-webkit-scrollbar-thumb { background: var(--border, #1e3928); border-radius: 2px; }

.pipeline-batch-name {
  font-size: 20px;
  font-weight: 700;
  color: var(--cyan, #2dd4bf);
  margin-bottom: 4px;
}

.pipeline-batch-count {
  font-size: 12px;
  color: var(--fg-dim, #4d6e56);
}

.pipeline-summary-row {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
  border-bottom: 1px solid rgba(30, 57, 40, 0.5);
}

.pipeline-summary-row:last-child { border-bottom: none; }

.pipeline-summary-label {
  color: var(--fg-dim, #4d6e56);
  font-size: 11px;
}

.pipeline-summary-value {
  color: var(--fg, #b5ccba);
  font-weight: 600;
  font-size: 12px;
}

/* Rejection reason bars */
.pipeline-reason-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.pipeline-reason-label {
  color: var(--fg-dim, #4d6e56);
  font-size: 11px;
  min-width: 140px;
  flex-shrink: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pipeline-reason-bar {
  flex: 1;
  height: 6px;
  background: var(--bg, #0c1310);
  border-radius: 3px;
  overflow: hidden;
}

.pipeline-reason-fill {
  display: block;
  height: 100%;
  background: var(--red, #f47067);
  border-radius: 3px;
  opacity: 0.6;
  transition: width 0.3s;
}

.pipeline-reason-count {
  color: var(--fg, #b5ccba);
  font-size: 11px;
  font-weight: 600;
  min-width: 40px;
  text-align: right;
}

/* ── Pipeline Status Badge ────────────────────── */

.pipeline-status {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
}
.pipeline-status.live    { color: var(--green, #56d364); }
.pipeline-status.loading { color: var(--fg-dim, #4d6e56); }
.pipeline-status.stale   { color: var(--yellow, #d4a72c); }
.pipeline-status.error   { color: var(--red, #f47067); }

/* ── Pipeline Title Row ───────────────────────── */

.pipeline-title-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.pipeline-batch-label {
  font-size: 12px;
  color: var(--fg-dim, #4d6e56);
  font-weight: 600;
}

.pipeline-batch-select {
  margin-left: 8px;
  font-size: 11px;
  padding: 2px 4px;
  background: var(--bg-2, #2a2a2a);
  color: var(--fg, #eee);
  border: 1px solid var(--border, #444);
  border-radius: 3px;
  cursor: pointer;
}
.pipeline-batch-select:hover { border-color: var(--accent, #6cf); }

.pipeline-stage.clickable { cursor: pointer; }
.pipeline-stage.clickable:hover { background: rgba(255,255,255,0.04); }

.pipeline-stage-modal {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  animation: pipelineModalFade 0.12s ease-out;
}
@keyframes pipelineModalFade {
  from { opacity: 0; }
  to { opacity: 1; }
}
.pipeline-stage-modal-panel {
  background: var(--bg-1, #1a1a1a);
  border: 1px solid var(--border, #333);
  border-radius: 6px;
  width: min(720px, 90vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0,0,0,0.5);
}
.pipeline-stage-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, #333);
}
.pipeline-stage-modal-header h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg, #eee);
  text-transform: capitalize;
}
.pipeline-stage-modal-close {
  background: none;
  border: none;
  color: var(--fg-dim, #888);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  padding: 0 6px;
}
.pipeline-stage-modal-close:hover { color: var(--fg, #eee); }
.pipeline-stage-modal-body {
  padding: 12px 16px;
  overflow-y: auto;
  font-size: 12px;
}
.pipeline-stage-modal-empty {
  color: var(--fg-dim, #888);
  font-style: italic;
  padding: 20px 0;
  text-align: center;
}
.pipeline-stage-modal-body table {
  width: 100%;
  border-collapse: collapse;
}
.pipeline-stage-modal-body th,
.pipeline-stage-modal-body td {
  padding: 6px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border, #2a2a2a);
}
.pipeline-stage-modal-body th {
  color: var(--fg-dim, #888);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.pipeline-stage-modal-body td.num,
.pipeline-stage-modal-body th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.pipeline-stage-modal-body tbody tr:hover {
  background: rgba(255,255,255,0.03);
}
.pipeline-stage-modal-detail {
  background: rgba(255,255,255,0.02);
}
.pipeline-stage-modal-detail td {
  padding: 8px 10px 10px 20px !important;
  font-size: 11px;
  border-bottom: 2px solid var(--border, #333) !important;
}
.pipeline-reason-label {
  color: var(--fg-dim, #888);
  margin-right: 6px;
}
.pipeline-reason-pill {
  display: inline-block;
  padding: 2px 8px;
  margin: 2px 3px;
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--border, #333);
  border-radius: 10px;
  font-size: 10px;
  color: var(--fg, #ccc);
  white-space: nowrap;
}

/* Scrape grouped state/source rows */
.pipeline-modal-state-row td {
  background: rgba(255,255,255,0.04);
  font-weight: 600;
  border-top: 1px solid var(--border, #333);
}
.pipeline-modal-state-label {
  color: var(--fg, #eee);
}
.pipeline-modal-state-total {
  color: var(--fg, #eee);
}
.pipeline-modal-source-row td {
  color: var(--fg-dim, #aaa);
}
.pipeline-modal-source-label {
  padding-left: 24px !important;
  font-family: var(--mono, monospace);
  font-size: 11px;
}

/* ── Pipeline Flow (spec layout) ─────────────── */

/* .pipeline-flow uses flex already via .pipeline-stages — this variant
   is used by the new dashboards.js mount function */
.pipeline-flow {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  overflow-x: auto;
  padding: 20px 0;
}

.pipeline-flow .pipeline-stage {
  min-width: 110px;
  flex: 1;
  max-width: 180px;
  position: relative;
}

.pipeline-flow .pipeline-arrow {
  flex-shrink: 0;
  width: 24px;
  text-align: center;
  color: var(--fg-dim, #4d6e56);
  font-size: 20px;
  padding-top: 24px;
}

/* Stage number (large count) */
.stage-number {
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 28px;
  font-weight: bold;
  color: var(--fg, #b5ccba);
  line-height: 1;
}

.stage-label {
  font-size: 11px;
  color: var(--fg-dim, #4d6e56);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 4px;
}

.stage-secondary {
  font-size: 12px;
  color: var(--fg-dim, #4d6e56);
  margin-top: 4px;
}

/* Active and success states for spec stage boxes */
.pipeline-flow .pipeline-stage.active {
  border-color: var(--green, #56d364);
  box-shadow: 0 0 12px color-mix(in srgb, var(--green, #56d364) 40%, transparent);
}

.pipeline-flow .pipeline-stage.success {
  border-color: var(--blue, #3fb950);
  box-shadow: 0 0 12px color-mix(in srgb, var(--blue, #3fb950) 40%, transparent);
}

/* ── Rejected Pills ───────────────────────────── */

.pipeline-rejected-pills,
.pipeline-states-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.rejected-pill {
  display: inline-block;
  background: var(--bg3, #1a2b22);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--fg-dim, #4d6e56);
}

.state-pill {
  display: inline-block;
  background: var(--bg3, #1a2b22);
  border: 1px solid var(--border, #1e3928);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--fg, #b5ccba);
}

/* ── Pipeline States Section ─────────────────── */

.pipeline-rejected {
  margin-top: 16px;
  background: var(--bg2, #121e18);
  border: 1px solid var(--red, #f47067);
  border-radius: 6px;
  padding: 12px;
}

.pipeline-states-section {
  margin-top: 16px;
  background: var(--bg2, #121e18);
  border: 1px solid var(--border, #1e3928);
  border-radius: 6px;
  padding: 12px;
}

/* Shared section header used by both views */
.pipeline-section-header {
  font-size: 11px;
  font-weight: 700;
  color: var(--fg-dim, #4d6e56);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.pipeline-section-header.rejected {
  color: var(--red, #f47067);
}

/* ── Pipeline Tabs (scraping | skiptrace) ──────────── */

.pipeline-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--border, #1e3928);
}

.pipeline-tab {
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 10px 16px;
  color: var(--fg-dim, #4d6e56);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.pipeline-tab:hover {
  color: var(--fg, #b5ccba);
}
.pipeline-tab.pipeline-tab-locked {
  pointer-events: none;
  opacity: 0.55;
  cursor: not-allowed;
}
.pipeline-tab.active {
  color: var(--fg, #b5ccba);
  border-bottom-color: var(--blue, #3fb950);
}

.pipeline-tab-label {
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.pipeline-tab-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 12px;
  line-height: 1;
  font-weight: 700;
}
.pipeline-tab-icon.pending  { color: var(--fg-dim, #4d6e56); }
.pipeline-tab-icon.running  { color: var(--blue, #3fb950); }
.pipeline-tab-icon.waiting  { color: var(--yellow, #d4a72c); }
.pipeline-tab-icon.success  { color: var(--green, #56d364); }
.pipeline-tab-icon.error    { color: var(--red, #f47067); }
.pipeline-tab-icon.blocked  { color: var(--yellow, #d4a72c); }
.pipeline-tab-icon.gated    { color: var(--fg-dim, #4d6e56); }

/* ── Pipeline View Container ──────────────────────── */

.pipeline-view {
  padding-top: 12px;
}

/* ── Stage icon (replaces stage-number for the new 2-tab layout) ── */

.stage-icon {
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 28px;
  line-height: 1;
  font-weight: 700;
  text-align: center;
}
.stage-icon.pending { color: var(--fg-dim, #4d6e56); }
.stage-icon.running { color: var(--blue, #3fb950); }
.stage-icon.waiting { color: var(--yellow, #d4a72c); }
.stage-icon.success { color: var(--green, #56d364); }
.stage-icon.error   { color: var(--red, #f47067); }
.stage-icon.blocked { color: var(--yellow, #d4a72c); }

/* Spin the running icon to convey motion — light touch, 2s per rotation */
.stage-icon.spin,
.pipeline-tab-icon.spin {
  animation: stage-icon-spin 2s linear infinite;
}
@keyframes stage-icon-spin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}

/* Per-state borders for the stage box (overrides default gray) */
.pipeline-flow .pipeline-stage.running {
  border-color: var(--blue, #3fb950);
  box-shadow: 0 0 10px color-mix(in srgb, var(--blue, #3fb950) 30%, transparent);
}
.pipeline-flow .pipeline-stage.waiting {
  border-color: var(--yellow, #d4a72c);
  box-shadow: 0 0 10px color-mix(in srgb, var(--yellow, #d4a72c) 25%, transparent);
}
.pipeline-flow .pipeline-stage.success {
  border-color: var(--green, #56d364);
  box-shadow: 0 0 10px color-mix(in srgb, var(--green, #56d364) 25%, transparent);
}
.pipeline-flow .pipeline-stage.error {
  border-color: var(--red, #f47067);
  box-shadow: 0 0 10px color-mix(in srgb, var(--red, #f47067) 35%, transparent);
}
.pipeline-flow .pipeline-stage.blocked {
  border-color: var(--yellow, #d4a72c);
}
.pipeline-flow .pipeline-stage.pending {
  border-color: var(--border, #1e3928);
}

/* ── Skiptrace gate indicator ───────────────────── */
/* Applied to the skipmatrix_csv stage box when pipeline_summary.skiptrace_gate
   === 'closed'. Dims the box + shows a lock glyph next to the label so it's
   clear the pipeline is parked (waiting for the user to release the gate),
   not failed. */
.pipeline-flow .pipeline-stage.stage-gated {
  opacity: 0.7;
}
.stage-gate-icon {
  margin-right: 4px;
  font-size: 12px;
  cursor: help;
}
.batch-label-gated {
  color: var(--yellow, #d4a72c);
}

.skiptrace-gate-control {
  margin-top: 16px;
  display: grid;
  grid-template-columns: max-content 1fr auto auto;
  align-items: center;
  gap: 8px 12px;
  background: var(--bg2, #121e18);
  border: 1px solid var(--border, #1e3928);
  border-radius: 6px;
  padding: 12px;
}
.skiptrace-gate-btn {
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 8px 14px;
  min-width: 172px;
  font-size: 12px;
  font-weight: 800;
  color: var(--bg, #0c1310);
  cursor: pointer;
}
.skiptrace-gate-btn:disabled {
  opacity: 0.62;
  cursor: default;
}
.skiptrace-gate-btn.gate-btn-unlock {
  background: var(--green, #56d364);
}
.skiptrace-gate-btn.gate-btn-relock {
  background: var(--yellow, #d4a72c);
  color: var(--bg, #0c1310);
}
.skiptrace-gate-btn.gate-btn-loading {
  background: var(--bg3, #1a2b22);
  border-color: var(--border, #1e3928);
  color: var(--fg-dim, #4d6e56);
}
.skiptrace-gate-subtitle {
  min-width: 0;
  color: var(--fg-dim, #4d6e56);
  font-size: 12px;
}
.skiptrace-gate-error {
  color: var(--red, #f47067);
  font-size: 12px;
}
.skiptrace-gate-retry {
  border: 1px solid var(--border, #1e3928);
  background: var(--bg3, #1a2b22);
  color: var(--fg, #b5ccba);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
}
.pipeline-gate-modal-body p {
  margin: 0;
  color: var(--fg, #b5ccba);
  font-size: 13px;
  line-height: 1.45;
}
.pipeline-gate-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px 16px;
}

/* Wrap the skiptrace flow to 2 rows when the 8 boxes don't fit one line */
.pipeline-view .pipeline-flow {
  flex-wrap: wrap;
  row-gap: 16px;
}

/* ── Skiptrace summary footer ─────────────────────── */

.pipeline-skiptrace-summary {
  margin-top: 16px;
  background: var(--bg2, #121e18);
  border: 1px solid var(--border, #1e3928);
  border-radius: 6px;
  padding: 12px;
}
.pipeline-skiptrace-summary-line {
  font-size: 12px;
  color: var(--fg, #b5ccba);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.pipeline-skiptrace-summary-line .sk-key {
  color: var(--fg-dim, #4d6e56);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-size: 10px;
  margin-right: 4px;
}
.pipeline-skiptrace-summary-line .sk-empty {
  color: var(--fg-dim, #4d6e56);
  font-style: italic;
}
`;

  // ── Foreclosure Pipeline board ────────────────────────────────
  // The richer desktop foreclosure view, lifted into the shared layer so it
  // renders on BOTH the Pentacle desktop (interactive: batch selector, skiptrace
  // gate, per-stage drill-down) and the Pi wall (display: same pipelines, stage
  // states, pills, summary — interactive controls hidden via the manifest).
  //
  // Stateful contract (mount/update/unmount): persistent listeners, in-place
  // updates, optimistic gate guard, and modals. Window/IPC coupling is injected
  // via `ctx.actions` (setBatchGate/refetch/refreshNow) so the shared lib never
  // references `window.cc` directly — the desktop adapter binds them to
  // chat_streamd IPC; the Pi leaves them unset (controls are display-only).
  //
  // Ported verbatim from pentacle/renderer/dashboards/foreclosure.js (the OG
  // desktop board) — same DOM, same class names, same behavior. CSS is the
  // verbatim desktop styling self-injected (with var() fallbacks so the Pi,
  // which lacks the desktop theme tokens, still renders correctly).
  registerBoard((function () {
    'use strict';

    const PIPELINES = [
      {
        id: 'scraping',
        label: 'Scraping',
        stages: [
          { id: 'scrape',     label: 'Scrape' },
          { id: 'cad',        label: 'CAD' },
          { id: 'propstream', label: 'PropStream' },
          { id: 'qualify',    label: 'Qualify' },
          { id: 'staging',    label: 'Staging' },
        ],
      },
      {
        id: 'skiptrace',
        label: 'Skiptrace',
        stages: [
          { id: 'skipmatrix_csv',          label: 'CSV' },
          { id: 'skipmatrix_submit',       label: 'Submit' },
          { id: 'skipmatrix_confirm',      label: 'Confirm' },
          { id: 'skipmatrix_invoice',      label: 'Invoice' },
          { id: 'skipmatrix_pay',          label: 'Pay' },
          { id: 'skipmatrix_paid_confirm', label: 'Paid' },
          { id: 'skipmatrix_results',      label: 'Results' },
          { id: 'prod_hydrate_promote',    label: 'Prod' },
        ],
      },
    ];

    const _STAGES_WITH_BREAKDOWN = ['scrape', 'cad', 'propstream', 'qualify', 'staging'];

    const _BREAKDOWN_COLUMNS = {
      scrape:     [{k:'state',label:'State / source'}, {k:'count',label:'Count',num:true}],
      cad:        [{k:'state',label:'State'}, {k:'done',label:'Done',num:true}, {k:'miss',label:'Miss',num:true}, {k:'pending',label:'Pending',num:true}, {k:'skipped',label:'Skipped',num:true}],
      propstream: [{k:'state',label:'State'}, {k:'done',label:'Done',num:true}, {k:'pending',label:'Pending',num:true}, {k:'skipped',label:'Skipped',num:true}],
      qualify:    [{k:'state',label:'State'}, {k:'qualified',label:'Qual.',num:true}, {k:'rejected',label:'Rej.',num:true}, {k:'top_reason',label:'Top reason'}],
      staging:    [{k:'state',label:'State'}, {k:'early_filings',label:'Early',num:true}, {k:'foreclosures',label:'Forecl.',num:true}, {k:'new',label:'New',num:true}, {k:'preexisting',label:'Pre',num:true}],
    };

    const STATE_MAP = {
      waiting:                 { icon: '○', cls: 'pending',  label: 'pending' },
      running:                 { icon: '◐', cls: 'running',  label: 'running' },
      waiting_email:           { icon: '✉', cls: 'waiting',  label: 'waiting for email' },
      complete:                { icon: '✓', cls: 'success',  label: 'complete' },
      failed:                  { icon: '✗', cls: 'error',    label: 'failed' },
      blocked_qa:              { icon: '!', cls: 'error',    label: 'blocked — QA' },
      blocked_circuit_breaker: { icon: '⏸', cls: 'blocked',  label: 'blocked — another batch in flight' },
    };

    function _stateMeta(state) {
      return STATE_MAP[state] || { icon: '○', cls: 'pending', label: state || 'pending' };
    }

    function _rollup(stageRows) {
      let hasError = false, hasRunning = false, hasWaitingEmail = false, hasPending = false;
      let allComplete = stageRows.length > 0;
      for (const r of stageRows) {
        const st = r ? r.state : 'waiting';
        if (st !== 'complete') allComplete = false;
        if (st === 'failed' || st === 'blocked_qa') hasError = true;
        else if (st === 'running') hasRunning = true;
        else if (st === 'waiting_email') hasWaitingEmail = true;
        else if (st === 'waiting' || st === 'blocked_circuit_breaker') hasPending = true;
      }
      if (hasError) return 'failed';
      if (allComplete) return 'complete';
      if (hasRunning) return 'running';
      if (hasWaitingEmail) return 'waiting_email';
      if (hasPending) return 'waiting';
      return 'waiting';
    }

    function _skiptraceExecuted(smStages) {
      return ['skipmatrix_paid_confirm', 'skipmatrix_results']
        .some((stage) => smStages && smStages[stage] && smStages[stage].state === 'complete');
    }

    function _stagesByName(stages) {
      const out = {};
      (stages || []).forEach(s => { out[s.stage] = s; });
      return out;
    }

    function _fmt(n) {
      if (n == null || n === '' || isNaN(n)) return '—';
      return Number(n).toLocaleString();
    }

    function _prodPromoteSummary(metrics) {
      const m = metrics || {};
      const promoted = m.created_owners || m.promoted || 0;
      const inProd = m.prod_total_count || m.promoted_total_count || 0;
      const excluded = m.stale_window_excluded || 0;
      if (inProd > 0) {
        if (promoted > 0) return `${_fmt(inProd)} in prod · ${_fmt(promoted)} new`;
        if (excluded > 0) return `${_fmt(inProd)} in prod · ${_fmt(excluded)} excluded`;
        return `${_fmt(inProd)} in prod`;
      }
      return `${_fmt(promoted)} promoted`;
    }

    function _elapsed(seconds) {
      if (seconds == null) return '';
      seconds = Math.max(0, Math.floor(seconds));
      if (seconds < 60) return `${seconds}s`;
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      return s > 0 ? `${m}m${s}s` : `${m}m`;
    }

    // Self-contained pill reconciler (ported from the desktop registry.js global
    // so the shared board has no renderer dependency). In-place update keyed by
    // a data-* attribute; adds/removes pills to match `items`.
    function _reconcilePills(container, items, keyAttr, formatFn) {
      const existing = new Map();
      container.querySelectorAll(`[data-${keyAttr}]`).forEach(el => {
        existing.set(el.dataset[keyAttr], el);
      });
      const seen = new Set();
      items.forEach(({ key, value }) => {
        seen.add(key);
        if (existing.has(key)) {
          existing.get(key).textContent = formatFn(key, value);
        } else {
          const pill = document.createElement('span');
          pill.className = keyAttr === 'reason' ? 'rejected-pill' : 'state-pill';
          pill.dataset[keyAttr] = key;
          pill.textContent = formatFn(key, value);
          container.appendChild(pill);
        }
      });
      existing.forEach((el, key) => {
        if (!seen.has(key)) el.remove();
      });
    }

    // ── mount ──
    function mount(container, ctx) {
      injectBoardCss('foreclosure', FORECLOSURE_CSS);
      const gateInteractive = ctx.isVisible('skiptrace-gate');
      const batchInteractive = ctx.isVisible('batch-select');
      const drillInteractive = ctx.isVisible('stage-drilldown');

      const root = document.createElement('div');
      // .foreclosure-shell wraps the OG .foreclosure-dashboard markup for
      // cross-surface sizing (100%/100%); the rest of the class names match the
      // desktop board verbatim so the ported CSS applies identically.
      root.className = 'foreclosure-shell foreclosure-dashboard';
      root.dataset.mode = ctx.mode;
      root.dataset.activeTab = 'scraping';
      root.dataset.userPinned = 'false';

      const header = document.createElement('div');
      header.className = 'pipeline-header';

      const titleRow = document.createElement('div');
      titleRow.className = 'pipeline-title-row';
      const title = document.createElement('h2');
      title.className = 'pipeline-title';
      title.textContent = 'Foreclosure Pipeline';
      const batchLabel = document.createElement('span');
      batchLabel.className = 'pipeline-batch-label';
      const batchSelect = document.createElement('select');
      batchSelect.className = 'pipeline-batch-select';
      batchSelect.style.display = 'none';
      if (batchInteractive) {
        batchSelect.addEventListener('change', () => {
          const picked = batchSelect.value || '';
          root.dataset.selectedBatch = picked;
          if (statusBadge) {
            statusBadge.textContent = 'Switching batch...';
            statusBadge.className = 'pipeline-status loading';
          }
          if (ctx.actions && typeof ctx.actions.refetch === 'function') ctx.actions.refetch();
        });
      }
      titleRow.appendChild(title);
      titleRow.appendChild(batchLabel);
      if (batchInteractive) titleRow.appendChild(batchSelect);

      const metaRow = document.createElement('div');
      metaRow.className = 'pipeline-meta';
      const statusBadge = document.createElement('span');
      statusBadge.className = 'pipeline-status loading';
      statusBadge.textContent = 'Loading...';
      const lastUpdated = document.createElement('span');
      lastUpdated.className = 'pipeline-updated';
      const retryBtn = document.createElement('button');
      retryBtn.className = 'sb-btn';
      retryBtn.textContent = 'Retry';
      retryBtn.style.display = 'none';
      retryBtn.style.fontSize = '10px';
      retryBtn.style.padding = '4px 10px';

      metaRow.appendChild(statusBadge);
      metaRow.appendChild(lastUpdated);
      metaRow.appendChild(retryBtn);

      header.appendChild(titleRow);
      header.appendChild(metaRow);

      const tabs = document.createElement('div');
      tabs.className = 'pipeline-tabs';
      const tabEls = {};
      PIPELINES.forEach(p => {
        const btn = document.createElement('button');
        btn.className = 'pipeline-tab';
        btn.dataset.pipelineId = p.id;
        const icon = document.createElement('span');
        icon.className = 'pipeline-tab-icon pending';
        icon.textContent = '○';
        const txt = document.createElement('span');
        txt.className = 'pipeline-tab-label';
        txt.textContent = p.label;
        btn.appendChild(icon);
        btn.appendChild(txt);
        btn.addEventListener('click', () => {
          if (btn.getAttribute('aria-disabled') === 'true') return;
          root.dataset.activeTab = p.id;
          root.dataset.userPinned = 'true';
          _refreshTabActive(tabEls, p.id);
          _refreshViewVisibility(viewEls, p.id);
        });
        tabs.appendChild(btn);
        tabEls[p.id] = { btn, icon, label: txt };
      });

      const viewEls = {};
      PIPELINES.forEach(p => {
        const view = document.createElement('div');
        view.className = 'pipeline-view';
        view.dataset.pipelineId = p.id;

        const flow = document.createElement('div');
        flow.className = 'pipeline-flow';

        const stageEls = {};
        p.stages.forEach((def, i) => {
          const box = document.createElement('div');
          box.className = 'pipeline-stage';
          box.dataset.stageId = def.id;

          const iconEl = document.createElement('div');
          iconEl.className = 'stage-icon pending';
          iconEl.textContent = '○';

          const lbl = document.createElement('div');
          lbl.className = 'stage-label';
          lbl.textContent = def.label;

          const sec = document.createElement('div');
          sec.className = 'stage-secondary';

          box.appendChild(iconEl);
          box.appendChild(lbl);
          box.appendChild(sec);

          // Drill-down is an interactiveOnly action — clickable on the desktop,
          // display-only on the Pi (the wall shows the stage, Pi-Control fires
          // the drill-down on its behalf).
          if (_STAGES_WITH_BREAKDOWN.includes(def.id) && drillInteractive) {
            box.classList.add('clickable');
            box.addEventListener('click', () => {
              const latest = root._latestData;
              if (!latest) return;
              const stageRow = (latest.pipeline_stages || []).find(x => x.stage === def.id);
              _openStageModal(def.id, def.label, stageRow ? (stageRow.metrics || {}) : {});
            });
          }

          flow.appendChild(box);
          stageEls[def.id] = { box, iconEl, labelEl: lbl, sec };

          if (i < p.stages.length - 1) {
            const arrow = document.createElement('div');
            arrow.className = 'pipeline-arrow';
            arrow.textContent = '→';
            flow.appendChild(arrow);
          }
        });

        view.appendChild(flow);

        if (p.id === 'scraping') {
          const rejectedBox = document.createElement('div');
          rejectedBox.className = 'pipeline-rejected';
          const rejectedHeader = document.createElement('div');
          rejectedHeader.className = 'pipeline-section-header rejected';
          rejectedHeader.textContent = 'Rejected — 0';
          const rejectedReasons = document.createElement('div');
          rejectedReasons.className = 'pipeline-rejected-pills';
          rejectedBox.appendChild(rejectedHeader);
          rejectedBox.appendChild(rejectedReasons);

          const statesBox = document.createElement('div');
          statesBox.className = 'pipeline-states-section';
          const statesHeader = document.createElement('div');
          statesHeader.className = 'pipeline-section-header';
          statesHeader.textContent = 'Qualified by State';
          const qualifiedStates = document.createElement('div');
          qualifiedStates.className = 'pipeline-states-pills';
          statesBox.appendChild(statesHeader);
          statesBox.appendChild(qualifiedStates);

          const auctionBox = document.createElement('div');
          auctionBox.className = 'pipeline-states-section';
          const auctionHeader = document.createElement('div');
          auctionHeader.className = 'pipeline-section-header';
          auctionHeader.textContent = 'Auction Dates';
          const auctionPills = document.createElement('div');
          auctionPills.className = 'pipeline-states-pills';
          auctionBox.appendChild(auctionHeader);
          auctionBox.appendChild(auctionPills);

          // Skiptrace gate is an interactiveOnly action — present/wired on the
          // desktop, omitted on the Pi (the lock state still shows via the tab
          // icon + stage-gated styling, which are display info).
          let gateBox = null, gateBtn = null, gateSubtitle = null, gateError = null, gateRetry = null;
          let payGateBox = null, payGateBtn = null, paySubtitle = null, payError = null, payRetry = null;
          if (gateInteractive) {
            gateBox = document.createElement('div');
            gateBox.className = 'skiptrace-gate-control';
            gateBtn = document.createElement('button');
            gateBtn.className = 'skiptrace-gate-btn';
            gateSubtitle = document.createElement('div');
            gateSubtitle.className = 'skiptrace-gate-subtitle';
            gateError = document.createElement('span');
            gateError.className = 'skiptrace-gate-error';
            gateError.style.display = 'none';
            gateRetry = document.createElement('button');
            gateRetry.className = 'skiptrace-gate-retry';
            gateRetry.textContent = 'Retry';
            gateRetry.style.display = 'none';
            gateBox.appendChild(gateBtn);
            gateBox.appendChild(gateSubtitle);
            gateBox.appendChild(gateError);
            gateBox.appendChild(gateRetry);

            // Payment gate — parallel to the skiptrace gate, reusing the same
            // CSS. Toggles auto_pay_skipmatrix: 'open' pre-authorizes auto-pay,
            // 'closed' routes payment through the mobile approval chat.
            payGateBox = document.createElement('div');
            payGateBox.className = 'skiptrace-gate-control';
            payGateBtn = document.createElement('button');
            payGateBtn.className = 'skiptrace-gate-btn';
            paySubtitle = document.createElement('div');
            paySubtitle.className = 'skiptrace-gate-subtitle';
            payError = document.createElement('span');
            payError.className = 'skiptrace-gate-error';
            payError.style.display = 'none';
            payRetry = document.createElement('button');
            payRetry.className = 'skiptrace-gate-retry';
            payRetry.textContent = 'Retry';
            payRetry.style.display = 'none';
            payGateBox.appendChild(payGateBtn);
            payGateBox.appendChild(paySubtitle);
            payGateBox.appendChild(payError);
            payGateBox.appendChild(payRetry);
          }

          view.appendChild(rejectedBox);
          view.appendChild(statesBox);
          view.appendChild(auctionBox);
          if (gateBox) view.appendChild(gateBox);
          if (payGateBox) view.appendChild(payGateBox);

          viewEls[p.id] = {
            view, stageEls, rejectedHeader, rejectedReasons, statesHeader, qualifiedStates,
            auctionHeader, auctionPills, gateBox, gateBtn, gateSubtitle, gateError, gateRetry,
            payGateBox, payGateBtn, paySubtitle, payError, payRetry,
          };
        } else {
          const summaryBox = document.createElement('div');
          summaryBox.className = 'pipeline-skiptrace-summary';
          const summaryLine = document.createElement('div');
          summaryLine.className = 'pipeline-skiptrace-summary-line';
          summaryBox.appendChild(summaryLine);
          view.appendChild(summaryBox);
          viewEls[p.id] = { view, stageEls, summaryLine };
        }

        root.appendChild(view);
      });

      root.appendChild(header);
      root.appendChild(tabs);
      PIPELINES.forEach(p => root.appendChild(viewEls[p.id].view));
      if (container) container.appendChild(root);

      _refreshTabActive(tabEls, 'scraping');
      _refreshViewVisibility(viewEls, 'scraping');

      const refs = {
        root, batchLabel, batchSelect, statusBadge, lastUpdated, retryBtn,
        tabEls, viewEls,
        _gateInteractive: gateInteractive,
        __ctx: ctx,
      };

      if (gateInteractive && ctx.actions) {
        const retryHandler = () => { if (typeof ctx.actions.refetch === 'function') ctx.actions.refetch(); };
        retryBtn.addEventListener('click', retryHandler);
        refs._retryHandler = retryHandler;
        const gateClickHandler = () => _handleGateButtonClick(refs, ctx);
        const gateRetryHandler = () => _retryGateChange(refs, ctx);
        viewEls.scraping.gateBtn.addEventListener('click', gateClickHandler);
        viewEls.scraping.gateRetry.addEventListener('click', gateRetryHandler);
        refs._gateClickHandler = gateClickHandler;
        refs._gateRetryHandler = gateRetryHandler;
        const payClickHandler = () => _handlePaymentGateButtonClick(refs, ctx);
        const payRetryHandler = () => _retryPaymentGateChange(refs, ctx);
        viewEls.scraping.payGateBtn.addEventListener('click', payClickHandler);
        viewEls.scraping.payRetry.addEventListener('click', payRetryHandler);
        refs._payClickHandler = payClickHandler;
        refs._payRetryHandler = payRetryHandler;
      }

      return refs;
    }

    function _refreshTabActive(tabEls, activeId) {
      Object.entries(tabEls).forEach(([id, t]) => {
        t.btn.classList.toggle('active', id === activeId);
      });
    }

    function _refreshViewVisibility(viewEls, activeId) {
      Object.entries(viewEls).forEach(([id, v]) => {
        v.view.style.display = id === activeId ? '' : 'none';
      });
    }

    function _setTabLocked(tab, locked) {
      if (!tab || !tab.btn) return;
      tab.btn.classList.toggle('pipeline-tab-locked', !!locked);
      if (locked) {
        tab.btn.setAttribute('aria-disabled', 'true');
        tab.btn.setAttribute('tabindex', '-1');
      } else {
        tab.btn.removeAttribute('aria-disabled');
        tab.btn.removeAttribute('tabindex');
      }
    }

    function _skiptraceGate(data) {
      const gate = data && data.pipeline_summary && data.pipeline_summary.skiptrace_gate;
      return gate === 'open' || gate === 'closed' ? gate : null;
    }

    const STALE_POLL_GUARD_MS = 30_000;

    function _applyOptimisticGate(refs, data) {
      const opt = refs && refs.root && refs.root._gateOptimistic;
      if (!opt) return data;
      if (Date.now() > opt.expiresAt) {
        refs.root._gateOptimistic = null;
        return data;
      }
      const summary = (data && data.pipeline_summary) || {};
      const incomingBatch = summary.state_machine_batch || (data && data.batch) || '';
      if (incomingBatch !== opt.batch) return data;
      if (summary.skiptrace_gate === opt.gate) {
        refs.root._gateOptimistic = null;
        return data;
      }
      return {
        ...data,
        pipeline_summary: { ...summary, skiptrace_gate: opt.gate },
      };
    }

    function _selectedBatch(root, data) {
      const summary = (data && data.pipeline_summary) || {};
      return String(
        (root && root.dataset && root.dataset.selectedBatch)
          || (data && data.default_batch)
          || summary.state_machine_batch
          || (data && data.batch)
          || ''
      ).trim();
    }

    function _isSelectedBatchMissing(root, data) {
      const selected = root && root.dataset && root.dataset.selectedBatch;
      return !!(selected && data && data._missing_batch && selected === data._missing_batch);
    }

    function _qualifiedWaitingCount(data) {
      if (data && data.qualified_actual_count != null && !isNaN(data.qualified_actual_count)) {
        return Number(data.qualified_actual_count);
      }
      const byState = data && data.staging_by_state;
      if (!byState || typeof byState !== 'object') return 0;
      return Object.values(byState).reduce((total, value) => {
        if (value != null && typeof value === 'object') {
          return total + Object.values(value).reduce((a, b) => a + (Number(b) || 0), 0);
        }
        return total + (Number(value) || 0);
      }, 0);
    }

    function _refreshGateControl(refs, data) {
      if (!refs._gateInteractive) return;
      const s = refs.viewEls.scraping;
      if (!s || !s.gateBtn) return;
      const selectedMissing = _isSelectedBatchMissing(refs.root, data);
      const actualGate = selectedMissing ? null : _skiptraceGate(data);
      const gate = actualGate;
      const pending = !!refs.root._gatePending;
      const err = refs.root._gateError || '';
      const count = _qualifiedWaitingCount(data);

      s.gateBtn.classList.remove('gate-btn-unlock', 'gate-btn-relock');

      if (!gate) {
        s.gateBtn.textContent = 'Loading Skiptrace Gate';
        s.gateBtn.disabled = true;
        s.gateBtn.classList.add('gate-btn-loading');
        s.gateSubtitle.textContent = selectedMissing
          ? 'Waiting for selected batch status...'
          : 'Waiting for batch status...';
      } else if (gate === 'closed') {
        s.gateBtn.textContent = pending ? 'Unlocking...' : '🔓 Unlock Skiptrace';
        s.gateBtn.disabled = pending;
        s.gateBtn.classList.remove('gate-btn-loading');
        s.gateBtn.classList.add('gate-btn-unlock');
        s.gateSubtitle.textContent = `~${_fmt(count)} qualified leads waiting at gate`;
      } else {
        s.gateBtn.textContent = pending ? 'Re-locking...' : '🔒 Re-lock Skiptrace';
        s.gateBtn.disabled = pending;
        s.gateBtn.classList.remove('gate-btn-loading');
        s.gateBtn.classList.add('gate-btn-relock');
        s.gateSubtitle.textContent = 'Dispatcher will halt new submissions';
      }

      s.gateError.textContent = err ? ` ${err}` : '';
      s.gateError.style.display = err ? '' : 'none';
      s.gateRetry.style.display = err ? '' : 'none';
    }

    function _retryGateChange(refs, ctx) {
      const gate = refs.root._gateRetryGate;
      if (!gate) return;
      return _submitGateChange(refs, gate, ctx);
    }

    function _handleGateButtonClick(refs, ctx) {
      if (refs.root._gatePending) return;
      const data = refs.root._latestData;
      if (_isSelectedBatchMissing(refs.root, data)) return;
      const gate = _skiptraceGate(data);
      if (gate === 'closed') {
        _openGateConfirmModal(refs, data, ctx);
        return;
      }
      if (gate === 'open') {
        return _submitGateChange(refs, 'closed', ctx);
      }
    }

    function _openGateConfirmModal(refs, data, ctx) {
      _closeGateModal();
      const batch = _selectedBatch(refs.root, data);
      const count = _qualifiedWaitingCount(data);

      const overlay = document.createElement('div');
      overlay.className = 'pipeline-stage-modal pipeline-gate-modal';
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) _closeGateModal();
      });

      const panel = document.createElement('div');
      panel.className = 'pipeline-stage-modal-panel pipeline-gate-modal-panel';

      const header = document.createElement('div');
      header.className = 'pipeline-stage-modal-header';
      const title = document.createElement('h3');
      title.textContent = `Unlock Skiptrace for batch ${batch}?`;
      const closeBtn = document.createElement('button');
      closeBtn.className = 'pipeline-stage-modal-close';
      closeBtn.textContent = '×';
      closeBtn.title = 'Close (Esc)';
      closeBtn.addEventListener('click', _closeGateModal);
      header.appendChild(title);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      const body = document.createElement('div');
      body.className = 'pipeline-stage-modal-body pipeline-gate-modal-body';
      const copy = document.createElement('p');
      copy.textContent = `This will release ~${_fmt(count)} qualified leads to SkipMatrix processing. SkipMatrix charges per submitted lead.`;
      body.appendChild(copy);
      panel.appendChild(body);

      const actions = document.createElement('div');
      actions.className = 'pipeline-gate-modal-actions';
      const cancel = document.createElement('button');
      cancel.className = 'sb-btn pipeline-gate-modal-cancel';
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', _closeGateModal);
      const unlock = document.createElement('button');
      unlock.className = 'sb-btn primary pipeline-gate-modal-confirm';
      unlock.textContent = 'Unlock';
      unlock.addEventListener('click', async () => {
        _closeGateModal();
        await _submitGateChange(refs, 'open', ctx);
      });
      actions.appendChild(cancel);
      actions.appendChild(unlock);
      panel.appendChild(actions);

      overlay.appendChild(panel);
      document.body.appendChild(overlay);
    }

    function _closeGateModal() {
      const m = document.querySelector('.pipeline-gate-modal');
      if (m) m.remove();
    }

    async function _submitGateChange(refs, gate, ctx) {
      const data = refs.root._latestData || {};
      if (_isSelectedBatchMissing(refs.root, data)) {
        refs.root._gateError = '';
        refs.root._gateRetryGate = null;
        _refreshGateControl(refs, data);
        return;
      }
      const batch = _selectedBatch(refs.root, data);
      if (!batch || (gate !== 'open' && gate !== 'closed')) {
        refs.root._gateError = 'invalid_request';
        refs.root._gateRetryGate = null;
        _refreshGateControl(refs, data);
        return;
      }
      refs.root._gatePending = true;
      refs.root._gateError = '';
      refs.root._gateRetryGate = null;
      _refreshGateControl(refs, data);
      try {
        const api = ctx.actions && ctx.actions.setBatchGate;
        const result = api
          ? await api(batch, gate)
          : { ok: false, error: 'setBatchGate unavailable' };
        if (!result || !result.ok) {
          refs.root._gateError = (result && result.error) || 'gate update failed';
          refs.root._gateRetryGate = gate;
          return;
        }
        refs.root._gatePending = false;
        const optimistic = {
          ...data,
          pipeline_summary: {
            ...(data.pipeline_summary || {}),
            skiptrace_gate: gate,
          },
        };
        refs.root._latestData = optimistic;
        update(refs, optimistic, ctx);
        refs.root._gateOptimistic = {
          batch,
          gate,
          expiresAt: Date.now() + STALE_POLL_GUARD_MS,
        };
        if (ctx.actions && typeof ctx.actions.refreshNow === 'function') ctx.actions.refreshNow();
      } catch (e) {
        refs.root._gateError = String((e && e.message) || e);
        refs.root._gateRetryGate = gate;
      } finally {
        refs.root._gatePending = false;
        _refreshGateControl(refs, refs.root._latestData || data);
      }
    }

    // ── Payment gate (auto_pay_skipmatrix) ──
    // Parallel to the skiptrace gate: 'open' = auto-pay pre-authorized, 'closed'
    // = the batch pauses at skipmatrix_pay for a mobile approval-chat decision.
    // Its own optimistic/pending/error state (root._pay*) so toggling one gate
    // never clobbers the other. Writes go through setBatchGate(batch, gate,
    // PAY_SETTING); enabling auto-pay is the approval-removing direction, so it
    // gets the modal-on-open confirm (mirrors the skiptrace unlock modal).
    const PAY_SETTING = 'auto_pay_skipmatrix';

    function _paymentGate(data) {
      const gate = data && data.pipeline_summary && data.pipeline_summary.payment_gate;
      return gate === 'open' || gate === 'closed' ? gate : null;
    }

    function _applyOptimisticPaymentGate(refs, data) {
      const opt = refs && refs.root && refs.root._payGateOptimistic;
      if (!opt) return data;
      if (Date.now() > opt.expiresAt) { refs.root._payGateOptimistic = null; return data; }
      const summary = (data && data.pipeline_summary) || {};
      const incomingBatch = summary.state_machine_batch || (data && data.batch) || '';
      if (incomingBatch !== opt.batch) return data;
      if (summary.payment_gate === opt.gate) { refs.root._payGateOptimistic = null; return data; }
      return { ...data, pipeline_summary: { ...summary, payment_gate: opt.gate } };
    }

    function _refreshPaymentGateControl(refs, data) {
      if (!refs._gateInteractive) return;
      const s = refs.viewEls.scraping;
      if (!s || !s.payGateBtn) return;
      const selectedMissing = _isSelectedBatchMissing(refs.root, data);
      const gate = selectedMissing ? null : _paymentGate(data);
      const pending = !!refs.root._payGatePending;
      const err = refs.root._payGateError || '';

      s.payGateBtn.classList.remove('gate-btn-unlock', 'gate-btn-relock');
      if (!gate) {
        s.payGateBtn.textContent = 'Loading Payment Gate';
        s.payGateBtn.disabled = true;
        s.payGateBtn.classList.add('gate-btn-loading');
        s.paySubtitle.textContent = selectedMissing
          ? 'Waiting for selected batch status...'
          : 'Waiting for batch status...';
      } else if (gate === 'closed') {
        // closed = auto-pay off = each payment approved from mobile.
        s.payGateBtn.textContent = pending ? 'Enabling...' : '🔓 Enable Auto-Pay';
        s.payGateBtn.disabled = pending;
        s.payGateBtn.classList.remove('gate-btn-loading');
        s.payGateBtn.classList.add('gate-btn-unlock');
        s.paySubtitle.textContent = 'SkipMatrix invoice pauses for your approval';
      } else {
        s.payGateBtn.textContent = pending ? 'Disabling...' : '🔒 Require Approval';
        s.payGateBtn.disabled = pending;
        s.payGateBtn.classList.remove('gate-btn-loading');
        s.payGateBtn.classList.add('gate-btn-relock');
        s.paySubtitle.textContent = 'Auto-pay ON — invoice charged automatically on arrival';
      }
      s.payError.textContent = err ? ` ${err}` : '';
      s.payError.style.display = err ? '' : 'none';
      s.payRetry.style.display = err ? '' : 'none';
    }

    function _retryPaymentGateChange(refs, ctx) {
      const gate = refs.root._payGateRetryGate;
      if (!gate) return;
      return _submitPaymentGateChange(refs, gate, ctx);
    }

    function _handlePaymentGateButtonClick(refs, ctx) {
      if (refs.root._payGatePending) return;
      const data = refs.root._latestData;
      if (_isSelectedBatchMissing(refs.root, data)) return;
      const gate = _paymentGate(data);
      if (gate === 'closed') { _openPaymentGateConfirmModal(refs, data, ctx); return; }
      if (gate === 'open') { return _submitPaymentGateChange(refs, 'closed', ctx); }
    }

    function _openPaymentGateConfirmModal(refs, data, ctx) {
      _closeGateModal();
      const batch = _selectedBatch(refs.root, data);

      const overlay = document.createElement('div');
      overlay.className = 'pipeline-stage-modal pipeline-gate-modal';
      overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeGateModal(); });

      const panel = document.createElement('div');
      panel.className = 'pipeline-stage-modal-panel pipeline-gate-modal-panel';

      const header = document.createElement('div');
      header.className = 'pipeline-stage-modal-header';
      const title = document.createElement('h3');
      title.textContent = `Enable auto-pay for batch ${batch}?`;
      const closeBtn = document.createElement('button');
      closeBtn.className = 'pipeline-stage-modal-close';
      closeBtn.textContent = '×';
      closeBtn.title = 'Close (Esc)';
      closeBtn.addEventListener('click', _closeGateModal);
      header.appendChild(title);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      const body = document.createElement('div');
      body.className = 'pipeline-stage-modal-body pipeline-gate-modal-body';
      const copy = document.createElement('p');
      copy.textContent = 'When this batch reaches payment, the SkipMatrix invoice amount will be charged automatically — no approval prompt. Leave this off to approve each payment from Pentacle mobile.';
      body.appendChild(copy);
      panel.appendChild(body);

      const actions = document.createElement('div');
      actions.className = 'pipeline-gate-modal-actions';
      const cancel = document.createElement('button');
      cancel.className = 'sb-btn pipeline-gate-modal-cancel';
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', _closeGateModal);
      const confirm = document.createElement('button');
      confirm.className = 'sb-btn primary pipeline-gate-modal-confirm';
      confirm.textContent = 'Enable Auto-Pay';
      confirm.addEventListener('click', async () => {
        _closeGateModal();
        await _submitPaymentGateChange(refs, 'open', ctx);
      });
      actions.appendChild(cancel);
      actions.appendChild(confirm);
      panel.appendChild(actions);

      overlay.appendChild(panel);
      document.body.appendChild(overlay);
    }

    async function _submitPaymentGateChange(refs, gate, ctx) {
      const data = refs.root._latestData || {};
      if (_isSelectedBatchMissing(refs.root, data)) {
        refs.root._payGateError = '';
        refs.root._payGateRetryGate = null;
        _refreshPaymentGateControl(refs, data);
        return;
      }
      const batch = _selectedBatch(refs.root, data);
      if (!batch || (gate !== 'open' && gate !== 'closed')) {
        refs.root._payGateError = 'invalid_request';
        refs.root._payGateRetryGate = null;
        _refreshPaymentGateControl(refs, data);
        return;
      }
      refs.root._payGatePending = true;
      refs.root._payGateError = '';
      refs.root._payGateRetryGate = null;
      _refreshPaymentGateControl(refs, data);
      try {
        const api = ctx.actions && ctx.actions.setBatchGate;
        const result = api
          ? await api(batch, gate, PAY_SETTING)
          : { ok: false, error: 'setBatchGate unavailable' };
        if (!result || !result.ok) {
          refs.root._payGateError = (result && result.error) || 'gate update failed';
          refs.root._payGateRetryGate = gate;
          return;
        }
        refs.root._payGatePending = false;
        const optimistic = {
          ...data,
          pipeline_summary: { ...(data.pipeline_summary || {}), payment_gate: gate },
        };
        refs.root._latestData = optimistic;
        update(refs, optimistic, ctx);
        refs.root._payGateOptimistic = {
          batch,
          gate,
          expiresAt: Date.now() + STALE_POLL_GUARD_MS,
        };
        if (ctx.actions && typeof ctx.actions.refreshNow === 'function') ctx.actions.refreshNow();
      } catch (e) {
        refs.root._payGateError = String((e && e.message) || e);
        refs.root._payGateRetryGate = gate;
      } finally {
        refs.root._payGatePending = false;
        _refreshPaymentGateControl(refs, refs.root._latestData || data);
      }
    }

    // ── update ──
    function update(refs, data, ctx) {
      const { root, batchLabel, batchSelect, statusBadge, tabEls, viewEls } = refs;
      data = _applyOptimisticGate(refs, data);
      data = _applyOptimisticPaymentGate(refs, data);

      const summary = data.pipeline_summary || {};
      const smBatch = summary.state_machine_batch || data.batch || '';
      const skiptraceGate = _skiptraceGate(data);
      const gateClosed = skiptraceGate === 'closed';
      const payGateClosed = _paymentGate(data) === 'closed';
      const smStages = _stagesByName(data.pipeline_stages);
      const skiptraceExecuted = _skiptraceExecuted(smStages);
      batchLabel.textContent = smBatch
        ? `Batch ${smBatch}${gateClosed && !skiptraceExecuted ? ' — gated at skiptrace' : ''}`
        : '';
      batchLabel.classList.toggle('batch-label-gated', gateClosed && !skiptraceExecuted);

      if (statusBadge && data._missing_batch) {
        const fallback = data.default_batch || data.batch || '';
        statusBadge.textContent = `No data yet for ${data._missing_batch} — showing ${fallback}`;
        statusBadge.className = 'pipeline-status warning';
      }

      if (batchSelect && Array.isArray(data.all_batches)) {
        const current = root.dataset.selectedBatch || '';
        const want = ['', ...data.all_batches];
        const have = Array.from(batchSelect.options).map(o => o.value);
        const same = want.length === have.length && want.every((v, i) => v === have[i]);
        if (!same) {
          batchSelect.innerHTML = '';
          want.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b;
            opt.textContent = b || 'Current';
            batchSelect.appendChild(opt);
          });
        }
        batchSelect.value = current;
        if (refs._batchInteractive !== false && batchSelect.parentNode) {
          batchSelect.style.display = data.all_batches.length > 1 ? '' : 'none';
        }
      }

      let currentPipelineId = null;
      PIPELINES.forEach(p => {
        const rows = p.stages.map(s => smStages[s.id]);
        const rollupState = _rollup(rows);
        const tabState = p.id === 'skiptrace' && skiptraceExecuted ? 'complete' : rollupState;
        const meta = _stateMeta(tabState);
        const tab = tabEls[p.id];
        if (p.id === 'skiptrace' && gateClosed && !skiptraceExecuted) {
          tab.icon.textContent = '🔒';
          tab.icon.className = 'pipeline-tab-icon gated';
          tab.btn.title = 'Skiptrace gated — unlock from the Scraping tab to release queued leads';
          _setTabLocked(tab, true);
        } else {
          tab.icon.textContent = meta.icon;
          tab.icon.className = `pipeline-tab-icon ${meta.cls}${tabState === 'running' ? ' spin' : ''}`;
          tab.btn.title = `${p.label}: ${meta.label}`;
          if (p.id === 'skiptrace') _setTabLocked(tab, false);
        }
        if (currentPipelineId == null && rollupState !== 'complete') {
          currentPipelineId = p.id;
        }
      });
      if (currentPipelineId == null) {
        currentPipelineId = PIPELINES[PIPELINES.length - 1].id;
      }
      if (gateClosed && !skiptraceExecuted) currentPipelineId = 'scraping';

      if (root.dataset.userPinned !== 'true' && root.dataset.activeTab !== currentPipelineId) {
        root.dataset.activeTab = currentPipelineId;
        _refreshTabActive(tabEls, currentPipelineId);
        _refreshViewVisibility(viewEls, currentPipelineId);
      }

      PIPELINES.forEach(p => {
        const v = viewEls[p.id];
        p.stages.forEach(def => {
          const row = smStages[def.id];
          const state = row ? row.state : 'waiting';
          const meta = _stateMeta(state);
          const el = v.stageEls[def.id];
          const gatedStage = def.id === 'skipmatrix_csv' && gateClosed && !skiptraceExecuted && state !== 'complete';
          const payGatedStage = def.id === 'skipmatrix_pay' && payGateClosed && !skiptraceExecuted && state !== 'complete';
          const anyGated = gatedStage || payGatedStage;

          el.iconEl.textContent = meta.icon;
          el.iconEl.className = `stage-icon ${meta.cls}${state === 'running' ? ' spin' : ''}`;

          el.box.classList.remove('active', 'success', 'blocked', 'pending', 'error', 'waiting', 'stage-gated');
          el.box.classList.add(meta.cls);
          el.box.classList.toggle('stage-gated', anyGated);
          if (el.labelEl) el.labelEl.textContent = anyGated ? `🔒 ${def.label}` : def.label;

          const errText = row && row.error ? row.error.slice(0, 200) : '';
          el.box.title = gatedStage
            ? 'Skiptrace gated — unlock from the Scraping tab to release queued leads'
            : payGatedStage
              ? 'Payment requires approval — enable auto-pay from the Scraping tab to charge automatically'
              : (errText || meta.label);

          el.sec.textContent = _secondaryText(def.id, row, data);
        });
      });

      root._latestData = data;

      const s = viewEls.scraping;
      const rej = data.rejected || 0;
      s.rejectedHeader.textContent = `Rejected — ${_fmt(rej)}`;
      const reasons = Object.entries(data.rejection_reasons || {})
        .sort((a,b) => b[1]-a[1]).slice(0,14)
        .map(([key,value]) => ({ key, value }));
      _reconcilePills(s.rejectedReasons, reasons, 'reason', (k,v) => `${k} (${v})`);

      const states = Object.entries(data.qualified_by_state || {})
        .sort((a,b) => b[1]-a[1])
        .map(([key,value]) => ({ key, value }));
      s.statesHeader.textContent = `Qualified by State — ${_fmt(data.qualified || 0)}`;
      _reconcilePills(s.qualifiedStates, states, 'state', (k,v) => `${k}: ${v}`);

      const auction = data.auction_date_buckets || {};
      const totalAuction = Object.values(auction).reduce((a,b) => a + (Number(b)||0), 0);
      s.auctionHeader.textContent = totalAuction > 0
        ? `Auction Dates — ${_fmt(totalAuction)} in staging`
        : 'Auction Dates';
      const bucketLabels = {
        'past':        'past',
        'within_2w':   '< 2w',
        '2_4w':        '2–4w',
        '4_8w':        '4–8w',
        '8w_6mo':      '8w–6mo',
        '6mo+':        '6mo+',
        'unparseable': 'unparsed',
      };
      const bucketOrder = ['past','within_2w','2_4w','4_8w','8w_6mo','6mo+','unparseable'];
      const auctionPills = bucketOrder
        .filter(k => (auction[k] || 0) > 0)
        .map(k => ({ key: k, value: auction[k] }));
      _reconcilePills(
        s.auctionPills, auctionPills, 'auction',
        (k, v) => `${bucketLabels[k] || k}: ${v}`,
      );
      _refreshGateControl(refs, data);
      _refreshPaymentGateControl(refs, data);

      const sk = viewEls.skiptrace;
      sk.summaryLine.innerHTML = _skiptraceSummary(smStages);
    }

    function _secondaryText(stageId, row, data) {
      const m = (row && row.metrics) || {};
      const st = row ? row.state : 'waiting';

      switch (stageId) {
        case 'scrape': {
          const q = data.scraper_queue || {};
          if (q.total > 0) {
            if (q.running_job) {
              return `${q.running_job.state}/${q.running_job.scraper_name} (${_elapsed(q.running_job.elapsed_seconds)})`;
            }
            const parts = [`${_fmt(q.completed)}/${_fmt(q.total)} done`];
            if (q.failed > 0) parts.push(`${_fmt(q.failed)} fail`);
            if (q.pending > 0) parts.push(`${_fmt(q.pending)} pending`);
            return parts.join(' · ');
          }
          const tot = m.total_scraped != null ? m.total_scraped : (data.scraped || 0);
          const dup = m.total_duped || 0;
          return dup > 0 ? `${_fmt(tot)} · ${_fmt(dup)} dupe` : `${_fmt(tot)} scraped`;
        }
        case 'cad': {
          const q = m.queue || (data.lead_pipeline_queue || {}).cad || {};
          if ((q.failed || 0) > 0 && !(q.pending || 0) && !(q.running || 0)) return `${_fmt(q.failed)} failed`;
          if ((q.running || 0) > 0) return `${_fmt(q.running)} running · ${_fmt(q.pending || 0)} pending`;
          const pending = data.cad_pending || 0;
          const done = data.cad_complete || m.cad_hydrated || 0;
          return pending > 0 ? `${_fmt(pending)} pending` : `${_fmt(done)} done`;
        }
        case 'propstream': {
          const q = m.queue || (data.lead_pipeline_queue || {}).propstream || {};
          if ((q.failed || 0) > 0 && !(q.pending || 0) && !(q.running || 0)) return `${_fmt(q.failed)} failed`;
          if ((q.running || 0) > 0) return `${_fmt(q.running)} running · ${_fmt(q.pending || 0)} pending`;
          const pending = data.ps_pending || 0;
          const done = data.ps_complete || m.propstream_hydrated || 0;
          return pending > 0 ? `${_fmt(pending)} pending` : `${_fmt(done)} done`;
        }
        case 'qualify': {
          const q = m.queue || (data.lead_pipeline_queue || {}).qualify || {};
          if ((q.failed || 0) > 0 && !(q.pending || 0) && !(q.running || 0)) return `${_fmt(q.failed)} failed`;
          const qual = data.qualified != null ? data.qualified : (m.qualified || 0);
          const rej  = m.rejected != null ? m.rejected : (data.rejected || 0);
          const pre  = data.qualified_preexisting_prod || 0;
          if (pre > 0) return `${_fmt(qual)} pass · ${_fmt(rej)} fail · ${_fmt(pre)} pre`;
          return `${_fmt(qual)} pass · ${_fmt(rej)} fail`;
        }
        case 'staging': {
          const newCount = (m.staged_new != null ? m.staged_new : null);
          const staged = newCount != null ? newCount : (data.qualified_actual_count || 0);
          const pre = m.preexisting_prod != null ? m.preexisting_prod : (data.qualified_preexisting_prod || 0);
          const early = m.staged_early_filings != null
            ? m.staged_early_filings
            : ((data.staging_by_source_family || {}).early_filings || 0);
          const foreclosures = m.staged_foreclosures != null
            ? m.staged_foreclosures
            : ((data.staging_by_source_family || {}).foreclosures || 0);
          const parts = [`${_fmt(staged)} new`];
          if (early > 0 || foreclosures > 0) {
            parts.push(`${_fmt(early)} early`);
            parts.push(`${_fmt(foreclosures)} forecl.`);
          }
          if (pre > 0) parts.push(`${_fmt(pre)} pre`);
          return parts.join(' · ');
        }
        case 'skipmatrix_csv': {
          const rows = m.csv_rows || 0;
          if (st === 'running') return `iter ${m.iterations || 1}…`;
          return rows > 0 ? `${_fmt(rows)} rows` : '';
        }
        case 'skipmatrix_submit': {
          if (st === 'complete') return 'submitted';
          if (st === 'running') return 'uploading';
          return '';
        }
        case 'skipmatrix_confirm':
          if (st === 'waiting_email') return 'awaiting email';
          if (st === 'complete') return 'confirmed';
          return '';
        case 'skipmatrix_invoice':
          if (m.amount_usd) return `$${Number(m.amount_usd).toFixed(2)}`;
          if (st === 'waiting_email') return 'awaiting email';
          return '';
        case 'skipmatrix_pay':
          if (st === 'running') return 'awaiting approval';
          if (st === 'complete') return 'paid';
          return '';
        case 'skipmatrix_paid_confirm':
          if (st === 'waiting_email') return 'awaiting email';
          if (st === 'complete') return 'confirmed';
          return '';
        case 'skipmatrix_results': {
          const hits = m.hit_count || 0;
          if (st === 'complete') return hits > 0 ? `${_fmt(hits)} hits` : 'done';
          if (st === 'waiting_email') return 'awaiting email';
          return '';
        }
        case 'prod_hydrate_promote': {
          if (st === 'complete') return _prodPromoteSummary(m);
          if (st === 'running')  return 'promoting…';
          return '';
        }
      }
      return '';
    }

    function _skiptraceSummary(smStages) {
      const csv = smStages.skipmatrix_csv;
      const invoice = smStages.skipmatrix_invoice;
      const results = smStages.skipmatrix_results;
      const prod = smStages.prod_hydrate_promote;

      const parts = [];
      const csvRows = csv && csv.metrics && csv.metrics.csv_rows;
      if (csvRows) parts.push(`<span class="sk-key">CSV</span> ${_fmt(csvRows)} rows`);

      const amt = invoice && invoice.metrics && invoice.metrics.amount_usd;
      if (amt) parts.push(`<span class="sk-key">Invoice</span> $${Number(amt).toFixed(2)}`);

      const hits = results && results.metrics && results.metrics.hit_count;
      if (hits != null) parts.push(`<span class="sk-key">Hits</span> ${_fmt(hits)}`);

      if (prod && prod.metrics) {
        parts.push(`<span class="sk-key">Prod</span> ${_prodPromoteSummary(prod.metrics)}`);
      }

      if (!parts.length) return '<span class="sk-empty">Waiting for skiptrace to start…</span>';
      return parts.join(' · ');
    }

    function _closeStageModal() {
      const m = document.querySelector('.pipeline-stage-modal');
      if (m) m.remove();
      document.removeEventListener('keydown', _modalKeydownHandler);
    }

    function _modalKeydownHandler(e) {
      if (e.key === 'Escape') _closeStageModal();
    }

    function _openStageModal(stageId, stageLabel, metrics) {
      _closeStageModal();

      const overlay = document.createElement('div');
      overlay.className = 'pipeline-stage-modal';
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) _closeStageModal();
      });

      const panel = document.createElement('div');
      panel.className = 'pipeline-stage-modal-panel';

      const header = document.createElement('div');
      header.className = 'pipeline-stage-modal-header';
      const title = document.createElement('h3');
      title.textContent = `${stageLabel || stageId} — by state`;
      const closeBtn = document.createElement('button');
      closeBtn.className = 'pipeline-stage-modal-close';
      closeBtn.textContent = '×';
      closeBtn.title = 'Close (Esc)';
      closeBtn.addEventListener('click', _closeStageModal);
      header.appendChild(title);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      const body = document.createElement('div');
      body.className = 'pipeline-stage-modal-body';
      const rows = Array.isArray(metrics.breakdown) ? metrics.breakdown : [];
      const cols = _BREAKDOWN_COLUMNS[stageId] || [];

      if (!rows.length) {
        const empty = document.createElement('div');
        empty.className = 'pipeline-stage-modal-empty';
        empty.textContent = 'No data yet for this stage.';
        body.appendChild(empty);
      } else if (stageId === 'scrape') {
        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const trh = document.createElement('tr');
        ['State / source', 'Count'].forEach((t, i) => {
          const th = document.createElement('th');
          th.textContent = t;
          if (i === 1) th.className = 'num';
          trh.appendChild(th);
        });
        thead.appendChild(trh);
        table.appendChild(thead);
        const tbody = document.createElement('tbody');
        rows.forEach(g => {
          const stateRow = document.createElement('tr');
          stateRow.className = 'pipeline-modal-state-row';
          const stateCell = document.createElement('td');
          stateCell.textContent = g.state;
          stateCell.className = 'pipeline-modal-state-label';
          const totalCell = document.createElement('td');
          totalCell.className = 'num pipeline-modal-state-total';
          totalCell.textContent = _fmt(g.total);
          stateRow.appendChild(stateCell);
          stateRow.appendChild(totalCell);
          tbody.appendChild(stateRow);
          (g.sources || []).forEach(src => {
            const tr = document.createElement('tr');
            tr.className = 'pipeline-modal-source-row';
            const stt = document.createElement('td');
            stt.textContent = src.source;
            stt.className = 'pipeline-modal-source-label';
            const ct = document.createElement('td');
            ct.className = 'num';
            ct.textContent = _fmt(src.count);
            tr.appendChild(stt);
            tr.appendChild(ct);
            tbody.appendChild(tr);
          });
        });
        table.appendChild(tbody);
        body.appendChild(table);
      } else {
        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const trh = document.createElement('tr');
        cols.forEach(c => {
          const th = document.createElement('th');
          th.textContent = c.label;
          if (c.num) th.className = 'num';
          trh.appendChild(th);
        });
        thead.appendChild(trh);
        table.appendChild(thead);
        const tbody = document.createElement('tbody');
        rows.forEach(r => {
          const tr = document.createElement('tr');
          cols.forEach(c => {
            const td = document.createElement('td');
            let v = r[c.k];
            if (c.num) td.className = 'num';
            if (v == null || v === '') v = '—';
            else if (c.num) v = _fmt(v);
            td.textContent = v;
            tr.appendChild(td);
          });
          tbody.appendChild(tr);

          if (stageId === 'qualify' && Array.isArray(r.reasons) && r.reasons.length) {
            const detail = document.createElement('tr');
            detail.className = 'pipeline-stage-modal-detail';
            const cell = document.createElement('td');
            cell.colSpan = cols.length;
            const pills = r.reasons
              .map(x => `<span class="pipeline-reason-pill">${x.reason} (${_fmt(x.count)})</span>`)
              .join(' ');
            cell.innerHTML = `<span class="pipeline-reason-label">All reasons:</span> ${pills}`;
            detail.appendChild(cell);
            tbody.appendChild(detail);
          }
        });
        table.appendChild(tbody);
        body.appendChild(table);
      }

      panel.appendChild(body);
      overlay.appendChild(panel);
      document.body.appendChild(overlay);
      document.addEventListener('keydown', _modalKeydownHandler);
    }

    function unmount(refs) {
      if (refs && refs.retryBtn && refs._retryHandler)
        refs.retryBtn.removeEventListener('click', refs._retryHandler);
      if (refs && refs.viewEls && refs.viewEls.scraping) {
        const s = refs.viewEls.scraping;
        if (s.gateBtn && refs._gateClickHandler) s.gateBtn.removeEventListener('click', refs._gateClickHandler);
        if (s.gateRetry && refs._gateRetryHandler) s.gateRetry.removeEventListener('click', refs._gateRetryHandler);
        if (s.payGateBtn && refs._payClickHandler) s.payGateBtn.removeEventListener('click', refs._payClickHandler);
        if (s.payRetry && refs._payRetryHandler) s.payRetry.removeEventListener('click', refs._payRetryHandler);
      }
      _closeGateModal();
      _closeStageModal();
    }

    return {
      id: 'foreclosure',
      name: 'Foreclosure Pipeline',
      description: 'Scraping + skiptrace pipeline with stage status, drill-down, distribution pills, and the skiptrace gate. Interactive controls (batch select, gate, drill-down) are desktop-only; the Pi renders display-mode.',
      color: '#56d364',
      manifest: {
        elements: [
          { id: 'pipeline-tabs', label: 'Scraping/Skiptrace tabs' },
          { id: 'pipeline-flow', label: 'Stage flow' },
          { id: 'distribution-pills', label: 'Rejected / qualified-by-state / auction pills' },
          { id: 'skiptrace-summary', label: 'Skiptrace summary' },
          { id: 'batch-select', label: 'Batch selector', interactiveOnly: true },
          { id: 'skiptrace-gate', label: 'Skiptrace gate control', interactiveOnly: true },
          { id: 'payment-gate', label: 'Payment gate control (auto-pay)', interactiveOnly: true },
          { id: 'stage-drilldown', label: 'Per-stage drill-down (click a stage)', interactiveOnly: true },
        ],
      },
      mount,
      update,
      unmount,
      _test: {
        _qualifiedWaitingCount,
        _skiptraceGate,
        _paymentGate,
        _selectedBatch,
        _submitGateChange,
        _submitPaymentGateChange,
        _applyOptimisticGate,
        _applyOptimisticPaymentGate,
        _rollup,
        _skiptraceExecuted,
        _stateMeta,
        STALE_POLL_GUARD_MS,
        PIPELINES,
      },
    };
  })());


  // ── Agent Stream (chat-stream) board ──────────────────────────
  // Merged Claude/Codex transcript stream across hosts. Stateful: session list
  // + selected-session timeline, with local session selection (pure navigation,
  // no platform writes — works identically on desktop and Pi display). Data
  // (events[]) comes from window.cc on the desktop / an SSE envelope on the Pi.
  // Ported verbatim from pentacle/renderer/dashboards/chat-stream.js (inline
  // styles, no external CSS).
  registerBoard((function () {
    'use strict';

    function _fmtTs(ts) {
      if (!ts) return '';
      try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
    }
    function _escape(text) {
      return String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    }
    function _kindColor(kind) {
      if (kind === 'USER') return '#79c0ff';
      if (kind === 'ASSIST') return '#56d364';
      if (kind === 'TOOL' || kind === 'TOOL-OUT') return '#d4a72c';
      if (kind === 'THINK') return '#b8a0fa';
      return '#9aa4af';
    }
    function _hostColor(host) {
      return '#2dd4bf';
    }
    function _groupSessions(events) {
      const sessions = new Map();
      for (const event of events || []) {
        const key = event.stream_id || `${event.host}:${event.provider}:${event.session_id || 'unknown'}`;
        if (!sessions.has(key)) {
          sessions.set(key, {
            key,
            host: event.host || 'unknown',
            provider: event.provider || 'unknown',
            sessionId: event.session_id || '',
            lastTimestamp: event.timestamp || '',
            lastText: event.text || '',
            lastKind: event.kind || 'EVENT',
            events: [],
          });
        }
        const session = sessions.get(key);
        session.events.push(event);
        session.lastTimestamp = event.timestamp || session.lastTimestamp;
        session.lastText = event.text || session.lastText;
        session.lastKind = event.kind || session.lastKind;
      }
      return Array.from(sessions.values()).sort((a, b) => String(b.lastTimestamp).localeCompare(String(a.lastTimestamp)));
    }
    function _renderSessionList(sessions) {
      if (!sessions.length) {
        return '<div style="padding:18px;border:1px dashed #2a3b33;border-radius:10px;color:#7f9187;">No chat-stream sessions yet.</div>';
      }
      return sessions.map((session) => `
        <div data-stream-id="${_escape(session.key)}" style="border:1px solid #24342d;border-radius:12px;padding:12px;background:#121a16;cursor:pointer;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <span style="font-size:11px;padding:3px 7px;border-radius:999px;background:${_hostColor(session.host)}22;color:${_hostColor(session.host)};text-transform:uppercase;">${_escape(session.host)}</span>
              <span style="font-size:11px;padding:3px 7px;border-radius:999px;background:#173126;color:#8ee4bf;text-transform:uppercase;">${_escape(session.provider)}</span>
            </div>
            <span style="font-size:11px;color:#93a39a;">${_fmtTs(session.lastTimestamp)}</span>
          </div>
          <div style="font-size:12px;color:#6f8478;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_escape(session.sessionId || session.key)}</div>
          <div style="font-size:13px;line-height:1.4;color:#e6eee9;">${_escape(session.lastText || '')}</div>
        </div>`).join('');
    }
    function _renderTimeline(events) {
      if (!events.length) {
        return '<div style="padding:18px;border:1px dashed #2a3b33;border-radius:10px;color:#7f9187;">Select a session to inspect its transcript.</div>';
      }
      return events.slice(-120).reverse().map((event) => `
        <div style="border:1px solid #222f29;border-radius:10px;padding:10px 12px;background:#151d19;">
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap;">
            <span style="font-size:11px;color:#93a39a;">${_fmtTs(event.timestamp)}</span>
            <span style="font-size:11px;color:${_kindColor(event.kind)};text-transform:uppercase;">${_escape(event.kind || 'EVENT')}</span>
          </div>
          <div style="white-space:pre-wrap;line-height:1.45;color:#edf3ef;">${_escape(event.text || '')}</div>
        </div>`).join('');
    }

    function mount(container, ctx) {
      if (container) container.innerHTML = '';
      const root = document.createElement('div');
      root.className = 'chat-stream-shell';
      root.dataset.mode = ctx.mode;
      root.style.cssText = 'padding:16px;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:13px;color:#d7e4dc;height:100%;display:flex;flex-direction:column;gap:14px;background:linear-gradient(180deg,#0f1713,#0d1310);box-sizing:border-box;';

      const header = document.createElement('div');
      header.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:16px;';
      header.innerHTML = `
        <div>
          <h2 style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:#f2fbf5;">Agent Stream</h2>
          <div style="font-size:13px;color:#8aa097;">Merged Claude and Codex transcript stream across configured hosts.</div>
        </div>
        <div data-role="status" style="font-size:12px;color:#8aa097;padding-top:4px;"></div>`;
      root.appendChild(header);

      const stats = document.createElement('div');
      stats.style.cssText = 'display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;';
      root.appendChild(stats);

      const layout = document.createElement('div');
      layout.style.cssText = 'display:grid;grid-template-columns:minmax(300px,360px) minmax(0,1fr);gap:14px;min-height:0;flex:1;';
      layout.innerHTML = `
        <div style="display:flex;flex-direction:column;min-height:0;">
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.6px;color:#6f8478;margin:0 0 8px 0;">Live Sessions</div>
          <div data-role="sessions" style="display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;padding-right:4px;"></div>
        </div>
        <div style="display:flex;flex-direction:column;min-height:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 8px 0;">
            <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.6px;color:#6f8478;">Transcript</div>
            <div data-role="selected-label" style="font-size:12px;color:#93a39a;"></div>
          </div>
          <div data-role="timeline" style="display:flex;flex-direction:column;gap:8px;overflow:auto;min-height:0;padding-right:4px;"></div>
        </div>`;
      root.appendChild(layout);

      if (container) container.appendChild(root);
      return {
        __ctx: ctx,
        status: header.querySelector('[data-role="status"]'),
        stats,
        sessions: layout.querySelector('[data-role="sessions"]'),
        timeline: layout.querySelector('[data-role="timeline"]'),
        selectedLabel: layout.querySelector('[data-role="selected-label"]'),
        selectedStreamId: null,
        lastSnapshot: null,
      };
    }

    function _renderStats(refs, sessions, events, connected) {
      const claudeCount = sessions.filter((s) => s.provider === 'claude').length;
      const codexCount = sessions.filter((s) => s.provider === 'codex').length;
      const hosts = new Set(sessions.map((s) => s.host)).size;
      const cards = [
        { label: 'Connection', value: connected ? 'Live' : 'Offline', color: connected ? '#56d364' : '#f47067' },
        { label: 'Hosts', value: String(hosts), color: '#79c0ff' },
        { label: 'Claude', value: String(claudeCount), color: '#a78bfa' },
        { label: 'Codex', value: String(codexCount), color: '#2dd4bf' },
      ];
      refs.stats.innerHTML = cards.map((card) => `
        <div style="border:1px solid #23342c;border-radius:12px;padding:12px;background:#131c17;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#7d9488;">${_escape(card.label)}</div>
          <div style="margin-top:6px;font-size:22px;font-weight:700;color:${card.color};">${_escape(card.value)}</div>
        </div>`).join('');
    }

    function update(refs, data, ctx) {
      if (!refs || !data) return;
      refs.lastSnapshot = data;
      const events = Array.isArray(data.events) ? data.events : [];
      const sessions = _groupSessions(events);

      if (!refs.selectedStreamId && sessions.length) {
        refs.selectedStreamId = sessions[0].key;
      }
      if (refs.selectedStreamId && !sessions.some((s) => s.key === refs.selectedStreamId)) {
        refs.selectedStreamId = sessions[0] ? sessions[0].key : null;
      }

      refs.status.textContent = data.connected ? 'Websocket connected' : 'Disconnected, showing cached state';
      refs.status.style.color = data.connected ? '#56d364' : '#f47067';
      _renderStats(refs, sessions, events, !!data.connected);

      refs.sessions.innerHTML = _renderSessionList(sessions);
      refs.sessions.querySelectorAll('[data-stream-id]').forEach((el) => {
        const active = el.getAttribute('data-stream-id') === refs.selectedStreamId;
        if (active) {
          el.style.borderColor = '#2dd4bf';
          el.style.background = '#15221d';
        }
        el.addEventListener('click', () => {
          refs.selectedStreamId = el.getAttribute('data-stream-id');
          update(refs, refs.lastSnapshot, refs.__ctx || ctx);
        });
      });

      const selected = sessions.find((s) => s.key === refs.selectedStreamId) || null;
      refs.selectedLabel.textContent = selected ? `${selected.host} · ${selected.provider}` : '';
      refs.timeline.innerHTML = _renderTimeline(selected ? selected.events : []);
    }

    function unmount(_refs) {}

    return {
      id: 'chat-stream',
      name: 'Agent Stream',
      description: 'Merged Claude and Codex event stream from configured hosts. Session selection is local navigation (no platform writes) so it works the same on desktop and Pi.',
      color: '#2dd4bf',
      manifest: {
        elements: [
          { id: 'stats', label: 'Connection/host/provider stats' },
          { id: 'sessions', label: 'Live session list' },
          { id: 'timeline', label: 'Selected-session transcript' },
        ],
      },
      mount,
      update,
      unmount,
      _test: { _groupSessions, _kindColor, _hostColor },
    };
  })());


  // ── UI Review board ───────────────────────────────────────────
  // Visual-QA artifact browser: stats, filter controls, artifact list, and an
  // iframe preview. Stateful (filters + selection). The filter controls are
  // interactiveOnly (a passive Pi wall doesn't get a search box / dropdowns);
  // everything else renders display-mode. Self-contained inline <style> (ported
  // verbatim from pentacle/renderer/dashboards/ui-review.js). No platform writes
  // — the artifact list comes from window.cc on the desktop.
  registerBoard((function () {
    'use strict';

    function _escape(value) {
      return String(value || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function _fmtTime(iso) {
      if (!iso) return '';
      try {
        return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      } catch (_) { return ''; }
    }
    function _unique(values) {
      return Array.from(new Set(values.filter(Boolean))).sort((a, b) => String(a).localeCompare(String(b)));
    }
    function _artifactKey(item) {
      return item && (item.artifactKey || item.id || item.entryUrl || item.url);
    }
    function _matchesFilters(item, filters) {
      const text = [item.title, item.repo, item.machine, item.summary, item.screen, item.artifactKey, ...(Array.isArray(item.tags) ? item.tags : [])]
        .join(' ').toLowerCase();
      const query = String(filters.query || '').trim().toLowerCase();
      if (query && !text.includes(query)) return false;
      if (filters.repo && item.repo !== filters.repo) return false;
      if (filters.machine && item.machine !== filters.machine) return false;
      if (filters.tag && !(Array.isArray(item.tags) && item.tags.includes(filters.tag))) return false;
      return true;
    }
    function _renderOptions(values, selected, label) {
      return [`<option value="">${_escape(label)}</option>`]
        .concat(values.map((value) => `<option value="${_escape(value)}" ${value === selected ? 'selected' : ''}>${_escape(value)}</option>`))
        .join('');
    }
    function _renderFilterControls(refs, artifacts, ctx) {
      const repos = _unique(artifacts.map((item) => item.repo));
      const machines = _unique(artifacts.map((item) => item.machine));
      const tags = _unique(artifacts.flatMap((item) => Array.isArray(item.tags) ? item.tags : []));
      refs.filtersEl.innerHTML = `
        <input data-filter="query" value="${_escape(refs.filters.query)}" placeholder="Search reviews">
        <select data-filter="repo">${_renderOptions(repos, refs.filters.repo, 'All repos')}</select>
        <select data-filter="machine">${_renderOptions(machines, refs.filters.machine, 'All machines')}</select>
        <select data-filter="tag">${_renderOptions(tags, refs.filters.tag, 'All tags')}</select>`;
      refs.filtersEl.querySelectorAll('[data-filter]').forEach((el) => {
        const onChange = () => { refs.filters[el.dataset.filter] = el.value; update(refs, refs.lastData, refs.__ctx || ctx); };
        el.addEventListener('input', onChange);
        el.addEventListener('change', onChange);
      });
    }
    function _renderArtifactList(artifacts, selectedId) {
      if (!artifacts.length) {
        return '<div class="ui-review-empty">No matching UI review artifacts found.</div>';
      }
      return artifacts.map((item) => {
        const active = _artifactKey(item) === selectedId;
        const dirty = item.source && item.source.dirty ? ' · dirty' : '';
        const labels = [item.repo, item.machine, _fmtTime(item.updatedAt)].filter(Boolean).join(' · ');
        const detail = [item.screen, ...(Array.isArray(item.tags) ? item.tags : [])].filter(Boolean).join(' · ');
        return `<button class="ui-review-artifact ${active ? 'active' : ''}" data-artifact-id="${_escape(_artifactKey(item))}">
          <span class="ui-review-artifact-title">${_escape(item.title || item.fileName || item.id)}</span>
          <span class="ui-review-artifact-meta">${_escape(labels)}${_escape(dirty)}</span>
          <span class="ui-review-artifact-path">${_escape(detail || item.summary || item.artifactKey || '')}</span>
        </button>`;
      }).join('');
    }
    function _renderStats(refs, data) {
      const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
      const repos = new Set(artifacts.map((item) => item.repo).filter(Boolean)).size;
      const machines = new Set(artifacts.map((item) => item.machine).filter(Boolean)).size;
      refs.stats.innerHTML = [
        { label: 'Artifacts', value: artifacts.length },
        { label: 'Repos', value: repos },
        { label: 'Machines', value: machines },
        { label: 'Indexed', value: _fmtTime(data.generatedAt) || 'now' },
      ].map((card) => `<div class="ui-review-stat"><span>${_escape(card.label)}</span><b>${_escape(card.value)}</b></div>`).join('');
    }
    function _selectArtifact(refs, artifact) {
      refs.selectedId = artifact ? _artifactKey(artifact) : null;
      refs.selected = artifact || null;
      refs.title.textContent = artifact ? artifact.title : 'Select an artifact';
      const sourceBits = artifact && artifact.source
        ? [artifact.source.gitCommit ? `commit ${artifact.source.gitCommit}` : '', artifact.source.dirty ? 'dirty worktree' : ''].filter(Boolean)
        : [];
      refs.meta.textContent = artifact
        ? [artifact.repo, artifact.machine, _fmtTime(artifact.updatedAt), ...sourceBits, artifact.summary || artifact.artifactKey].filter(Boolean).join(' · ')
        : 'UI review artifacts are static HTML snapshots generated by each repo.';
      const url = artifact && (artifact.entryUrl || artifact.url);
      refs.open.href = url || '#';
      refs.open.style.pointerEvents = artifact ? '' : 'none';
      refs.open.style.opacity = artifact ? '1' : '0.45';
      refs.preview.innerHTML = artifact
        ? `<iframe sandbox="allow-scripts allow-same-origin" title="${_escape(artifact.title || artifact.id)}" src="${_escape(url)}"></iframe>`
        : '<div class="ui-review-empty large">Choose a UI review from the list.</div>';
    }

    const UI_REVIEW_STYLE = `
      .ui-review-dashboard { height:100%; min-height:0; display:flex; flex-direction:column; gap:14px; padding:16px; color:#dce8e1; background:linear-gradient(180deg,#0d1511,#0a100d); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; box-sizing:border-box; }
      .ui-review-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
      .ui-review-head h2 { margin:0 0 5px; color:#f0f8f3; font-size:20px; }
      .ui-review-head p { margin:0; color:#8fa49a; font-size:13px; line-height:1.4; }
      .ui-review-status { color:#7ef0ba; font-size:12px; padding-top:4px; white-space:nowrap; }
      .ui-review-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
      .ui-review-filters { display:grid; grid-template-columns:minmax(180px,1.4fr) repeat(3,minmax(120px,1fr)); gap:8px; }
      .ui-review-filters input, .ui-review-filters select { min-width:0; border:1px solid #243a31; border-radius:7px; background:#101a16; color:#dce8e1; padding:8px 9px; font:12px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; }
      .ui-review-stat { border:1px solid #23382f; border-radius:8px; background:#101a16; padding:10px 12px; }
      .ui-review-stat span { display:block; color:#80958a; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.08em; }
      .ui-review-stat b { display:block; margin-top:5px; color:#f0f8f3; font-size:18px; }
      .ui-review-layout { min-height:0; flex:1; display:grid; grid-template-columns:minmax(260px,340px) minmax(0,1fr); gap:14px; }
      .ui-review-list { min-height:0; overflow:auto; display:flex; flex-direction:column; gap:8px; padding-right:4px; }
      .ui-review-artifact { width:100%; text-align:left; border:1px solid #243a31; border-radius:8px; background:#101a16; color:inherit; padding:11px; cursor:pointer; }
      .ui-review-artifact.active { border-color:#7ef0ba; background:#13231d; }
      .ui-review-artifact-title { display:block; color:#f0f8f3; font-size:13px; font-weight:800; line-height:1.3; }
      .ui-review-artifact-meta, .ui-review-artifact-path { display:block; margin-top:5px; color:#8fa49a; font-size:11px; line-height:1.35; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .ui-review-artifact-path { color:#5f776d; font-family:Menlo,Consolas,monospace; }
      .ui-review-stage { min-width:0; min-height:0; display:flex; flex-direction:column; border:1px solid #243a31; border-radius:10px; overflow:hidden; background:#07110d; }
      .ui-review-stage-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding:12px 14px; border-bottom:1px solid #243a31; background:#101a16; }
      .ui-review-stage-title { min-width:0; }
      .ui-review-stage-title b { display:block; color:#f0f8f3; font-size:14px; line-height:1.3; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .ui-review-stage-title span { display:block; margin-top:4px; color:#80958a; font:11px/1.35 Menlo,Consolas,monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .ui-review-open { flex:none; border:1px solid #315847; border-radius:6px; background:#113025; color:#f0f8f3; padding:7px 10px; text-decoration:none; font-size:12px; font-weight:800; }
      .ui-review-preview { min-height:0; flex:1; background:#050907; }
      .ui-review-preview iframe { display:block; width:100%; height:100%; border:0; background:#07110d; }
      .ui-review-empty { border:1px dashed #2a3d35; border-radius:8px; padding:14px; color:#80958a; font-size:13px; line-height:1.45; }
      .ui-review-empty.large { height:100%; display:flex; align-items:center; justify-content:center; border:0; }
      .ui-review-dashboard[data-mode="display"] .ui-review-filters { display:none; }
      @media (max-width: 980px) { .ui-review-layout { grid-template-columns:1fr; } .ui-review-stats, .ui-review-filters { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    `;

    function mount(container, ctx) {
      if (container) container.innerHTML = '';
      const root = document.createElement('div');
      root.className = 'ui-review-dashboard';
      root.dataset.mode = ctx.mode;
      root.innerHTML = `
        <style>${UI_REVIEW_STYLE}</style>
        <div class="ui-review-head">
          <div>
            <h2>UI Review</h2>
            <p>Reusable visual QA artifacts published by repos across Triforce. New screens and assets flow through Dashboard Hub without Pentacle app updates.</p>
          </div>
          <div class="ui-review-status" data-role="status">Loading...</div>
        </div>
        <div class="ui-review-stats" data-role="stats"></div>
        <div class="ui-review-filters" data-role="filters"></div>
        <div class="ui-review-layout">
          <div class="ui-review-list" data-role="list"></div>
          <div class="ui-review-stage">
            <div class="ui-review-stage-head">
              <div class="ui-review-stage-title">
                <b data-role="title">Select an artifact</b>
                <span data-role="meta">UI review artifacts are static HTML snapshots generated by each repo.</span>
              </div>
              <a class="ui-review-open" data-role="open" href="#" target="_blank" rel="noreferrer">Open</a>
            </div>
            <div class="ui-review-preview" data-role="preview"></div>
          </div>
        </div>`;
      if (container) container.appendChild(root);
      return {
        __ctx: ctx,
        root,
        status: root.querySelector('[data-role="status"]'),
        stats: root.querySelector('[data-role="stats"]'),
        filtersEl: root.querySelector('[data-role="filters"]'),
        list: root.querySelector('[data-role="list"]'),
        title: root.querySelector('[data-role="title"]'),
        meta: root.querySelector('[data-role="meta"]'),
        open: root.querySelector('[data-role="open"]'),
        preview: root.querySelector('[data-role="preview"]'),
        selectedId: null,
        selected: null,
        filters: { query: '', repo: '', machine: '', tag: '' },
        lastData: null,
      };
    }

    function update(refs, data, ctx) {
      if (!refs || !data) return;
      const cx = refs.__ctx || ctx;
      refs.lastData = data;
      const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
      const visibleArtifacts = artifacts.filter((item) => _matchesFilters(item, refs.filters));
      const stale = data._transport_stale || data._data_stale;
      const source = data.source === 'local-fallback' ? 'local fallback' : data.source === 'hub' ? 'hub' : 'hub missing';
      refs.status.textContent = data.error
        ? data.error
        : `${artifacts.length} artifact${artifacts.length === 1 ? '' : 's'} · ${source}${stale ? ' · stale' : ''}`;
      refs.status.style.color = data.error ? '#f47067' : stale ? '#d4a72c' : '#7ef0ba';
      _renderStats(refs, data);
      // Filter controls are interactiveOnly — desktop only; the Pi wall is passive.
      if (cx && cx.isVisible('filters')) _renderFilterControls(refs, artifacts, cx);
      if (refs.selectedId && !visibleArtifacts.some((item) => _artifactKey(item) === refs.selectedId)) {
        refs.selectedId = null;
        refs.selected = null;
      }
      if (!refs.selectedId && visibleArtifacts.length) {
        refs.selectedId = _artifactKey(visibleArtifacts[0]);
        refs.selected = visibleArtifacts[0];
      }
      refs.list.innerHTML = artifacts.length
        ? _renderArtifactList(visibleArtifacts, refs.selectedId)
        : '<div class="ui-review-empty">No UI review artifacts found. Publish an artifact bundle through the UI Review publisher, or enable local fallback for development.</div>';
      refs.list.querySelectorAll('[data-artifact-id]').forEach((button) => {
        button.addEventListener('click', () => {
          const artifact = visibleArtifacts.find((item) => _artifactKey(item) === button.dataset.artifactId);
          _selectArtifact(refs, artifact || null);
          update(refs, data, cx);
        });
      });
      _selectArtifact(refs, visibleArtifacts.find((item) => _artifactKey(item) === refs.selectedId) || null);
    }

    function unmount(_refs) {}

    return {
      id: 'ui-review',
      name: 'UI Review',
      description: 'Visual QA artifacts from repos across Triforce. Filter controls are desktop-interactive; the Pi renders the list + preview display-only.',
      color: '#7ef0ba',
      manifest: {
        elements: [
          { id: 'stats', label: 'Artifact stats' },
          { id: 'filters', label: 'Search / repo / machine / tag filters', interactiveOnly: true },
          { id: 'list', label: 'Artifact list' },
          { id: 'preview', label: 'Artifact preview' },
        ],
      },
      mount,
      update,
      unmount,
      _test: { _matchesFilters, _artifactKey, _unique },
    };
  })());


  registerBoard((function () {
    'use strict';

    const TERMINAL_STATES = ['acked', 'answered', 'spawned', 'resolved', 'expired', 'done', 'failed'];
    const SEVERITY_COLORS = {
      info: { bg: '#1d2640', fg: '#a8b8ff', border: '#3a4a7a' },
      warning: { bg: '#3a2614', fg: '#f5b78a', border: '#7a5326' },
      critical: { bg: '#3a1616', fg: '#f47067', border: '#7a2a2a' },
    };
    function esc(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function isTerminal(state) { return TERMINAL_STATES.includes(String(state || '')); }
    function cssEscape(value) {
      if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value);
      return String(value).replace(/["\\[\]]/g, '\\$&');
    }

    function mount(container, ctx) {
      if (container) container.innerHTML = '';
      const root_ = document.createElement('div');
      root_.className = 'notifications-shell';
      root_.dataset.mode = ctx.mode;
      root_.style.cssText = 'height:100%;box-sizing:border-box;display:flex;flex-direction:column;background:linear-gradient(180deg,#0f1713,#0d1310);color:var(--text,#d7e4dc);font-family:var(--font-mono,-apple-system,Helvetica,Arial,sans-serif);padding:12px;';

      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:0 4px 10px 4px;flex-shrink:0;';
      const heading = document.createElement('div');
      heading.style.cssText = 'font-size:13px;text-transform:uppercase;letter-spacing:0.6px;color:#e6eee9;font-weight:600;';
      heading.textContent = 'Notifications';
      header.appendChild(heading);

      let toggle = null;
      if (ctx.isVisible('show-resolved-toggle')) {
        const toggleWrap = document.createElement('label');
        toggleWrap.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;color:#8aa097;cursor:pointer;';
        toggle = document.createElement('input');
        toggle.type = 'checkbox';
        toggle.setAttribute('data-role', 'show-resolved');
        const toggleText = document.createElement('span');
        toggleText.textContent = 'Show resolved';
        toggleWrap.appendChild(toggle);
        toggleWrap.appendChild(toggleText);
        header.appendChild(toggleWrap);
      }
      root_.appendChild(header);

      const list = document.createElement('div');
      list.setAttribute('data-role', 'notification-list');
      list.style.cssText = 'display:flex;flex-direction:column;gap:8px;flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;';
      root_.appendChild(list);

      if (container) container.appendChild(root_);

      const refs = {
        __ctx: ctx,
        root: root_,
        list,
        toggle,
        notifications: [],
        showResolved: false,
      };

      if (toggle) {
        toggle.addEventListener('change', () => {
          refs.showResolved = !!toggle.checked;
          if (ctx.actions && typeof ctx.actions.refreshNow === 'function') ctx.actions.refreshNow();
        });
      }

      renderList(refs, ctx);
      return refs;
    }

    function unmount(_refs) {}

    function update(refs, data, ctx) {
      if (!refs || !data) return;
      refs.notifications = Array.isArray(data.notifications) ? data.notifications : [];
      renderList(refs, refs.__ctx || ctx);
    }

    function renderList(refs, ctx) {
      const rows = (refs.notifications || []).slice();
      rows.sort((a, b) => {
        const at = isTerminal(a && a.state) ? 1 : 0;
        const bt = isTerminal(b && b.state) ? 1 : 0;
        if (at !== bt) return at - bt;
        return String((b && b.created_at) || '').localeCompare(String((a && a.created_at) || ''));
      });

      if (rows.length === 0) {
        refs.list.innerHTML = `<div style="padding:14px;border:1px dashed #2a3b33;border-radius:8px;color:#5e6d65;font-size:12px;text-align:center;">No notifications</div>`;
        return;
      }

      const showActions = !ctx || ctx.isVisible('actions');
      refs.list.innerHTML = rows.map((n) => _cardHtml(n, showActions)).join('');
      if (showActions) _attachActions(refs, refs.list, ctx);
    }

    function _cardHtml(n, showActions) {
      const terminal = isTerminal(n.state);
      const sev = SEVERITY_COLORS[String(n.severity || 'info')] || SEVERITY_COLORS.info;
      const opacity = terminal ? '0.55' : '1';
      const sevBadge = `<span data-role="severity-badge" style="font-size:10px;padding:2px 8px;border-radius:999px;background:${sev.bg};color:${sev.fg};border:1px solid ${sev.border};text-transform:uppercase;letter-spacing:0.5px;">${esc(n.severity || 'info')}</span>`;
      const stateBadge = `<span data-role="state-badge" style="font-size:10px;padding:2px 8px;border-radius:999px;background:#202a25;color:#aab8b0;text-transform:uppercase;letter-spacing:0.5px;">${esc(n.state || '')}</span>`;
      const body = n.body
        ? `<div data-role="body" style="margin-top:6px;font-size:12px;color:#c2cfc7;line-height:1.4;white-space:pre-wrap;">${esc(n.body)}</div>`
        : '';
      const meta = `<div style="margin-top:6px;font-size:11px;color:#7d9488;display:flex;gap:10px;flex-wrap:wrap;">
          <span data-role="producer">${esc(n.producer || '')}</span>
          <span data-role="created-at">${esc(n.created_at || '')}</span>
        </div>`;
      const statusHtml = _statusHtml(n);
      const isQuestion = _isQuestionNotification(n);
      const actionsHtml = isQuestion
        ? _questionHtml(n, showActions && !terminal && n.state !== 'running')
        : ((terminal || n.state === 'running' || !showActions) ? '' : _actionsHtml(n));
      const errorEl = `<div data-role="resolve-error" data-id="${esc(n.notification_id)}" style="display:none;color:#f47067;font-size:11px;margin-top:6px;"></div>`;

      return `<div class="notification-row" data-notification-id="${esc(n.notification_id)}" data-state="${esc(n.state || '')}" style="border:1px solid #24342d;background:#121a16;border-radius:8px;padding:10px 12px;opacity:${opacity};">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          ${sevBadge}${stateBadge}
        </div>
        <div data-role="title" style="margin-top:6px;font-size:14px;color:#f2fbf5;font-weight:600;line-height:1.25;">${esc(n.title || '(untitled)')}</div>
        ${body}
        ${meta}
        ${statusHtml}
        ${actionsHtml}
        ${errorEl}
      </div>`;
    }

    function _statusHtml(n) {
      const state = String((n && n.state) || '');
      const result = n && n.resolution && n.resolution.result ? n.resolution.result : {};
      if (state === 'running') {
        return '<div data-role="command-status" style="margin-top:8px;font-size:12px;color:#f5b78a;">running…</div>';
      }
      if (state === 'done') {
        const stdout = result.stdout_tail ? `<pre data-role="stdout-tail" style="margin:6px 0 0 0;white-space:pre-wrap;color:#c2cfc7;font-family:inherit;font-size:12px;">${esc(result.stdout_tail)}</pre>` : '';
        return `<div data-role="command-status" style="margin-top:8px;font-size:12px;color:#8ee4bf;">✓ done${stdout}</div>`;
      }
      if (state === 'failed') {
        const stderr = result.stderr_tail ? `<pre data-role="stderr-tail" style="margin:6px 0 0 0;white-space:pre-wrap;color:#f6a3a3;font-family:inherit;font-size:12px;">${esc(result.stderr_tail)}</pre>` : '';
        const investigating = result.spawned_stream_id ? `<div data-role="investigating" style="margin-top:4px;color:#f5b78a;">investigating: ${esc(result.spawned_stream_id)}</div>` : '';
        return `<div data-role="command-status" style="margin-top:8px;font-size:12px;color:#f47067;">✗ failed${stderr}${investigating}</div>`;
      }
      const resolution = n && n.resolution ? n.resolution : {};
      if (state === 'answered') return _decisionHtml(`Answered: ${_choiceLabel(resolution.choice) || 'Unknown'}`, resolution);
      if (state === 'acked') return _decisionHtml('Acknowledged', resolution);
      if (state === 'spawned') return _decisionHtml(`Spawned: ${resolution.spawned_stream_id ? esc(resolution.spawned_stream_id) : 'worker'}`, resolution);
      if (state === 'resolved') {
        const label = resolution.action_kind === 'dismiss' || resolution.action_kind === 'dismissed' ? 'Dismissed' : 'Resolved';
        return _decisionHtml(label, resolution);
      }
      return '';
    }

    function _choiceLabel(choice) {
      if (choice === true || choice === 'true' || choice === 'yes') return 'Yes';
      if (choice === false || choice === 'false' || choice === 'no') return 'No';
      return choice == null ? '' : esc(choice);
    }

    function _decisionHtml(label, resolution) {
      const meta = [];
      if (resolution && resolution.by) meta.push(`by ${esc(resolution.by)}`);
      if (resolution && resolution.at) meta.push(esc(resolution.at));
      const metaHtml = meta.length ? ` <span data-role="decision-meta" style="color:#7d9488;">(${meta.join(' · ')})</span>` : '';
      return `<div data-role="decision" style="margin-top:8px;font-size:12px;color:#8ee4bf;">${label}${metaHtml}</div>`;
    }

    function _isQuestionNotification(n) {
      return !!(n && n.producer === 'agent_question.v1' && n.question && typeof n.question === 'object');
    }

    function _questionOptions(q) {
      return Array.isArray(q && q.options)
        ? q.options.filter((o) => o && typeof o === 'object')
        : [];
    }

    function _questionOptionValue(option, fallback) {
      const value = option && option.value;
      return value == null ? fallback : String(value);
    }

    function _questionOptionLabel(option, fallback) {
      const label = option && option.label;
      if (label != null && String(label)) return String(label);
      return _questionOptionValue(option, fallback);
    }

    function _questionLabelForValue(q, value) {
      const options = _questionOptions(q);
      const found = options.find((o, idx) => _questionOptionValue(o, `option-${idx + 1}`) === value);
      return found ? _questionOptionLabel(found, value) : value;
    }

    function _questionAnswerHtml(q) {
      const answer = q && q.answer && typeof q.answer === 'object' ? q.answer : null;
      const selections = answer && Array.isArray(answer.selections)
        ? answer.selections.map((v) => String(v))
        : [];
      const text = answer && typeof answer.text === 'string' && answer.text
        ? answer.text
        : '';
      const selected = selections.length
        ? selections.map((v) => esc(_questionLabelForValue(q, v))).join(', ')
        : (text ? esc(text) : 'No selection recorded');
      const note = answer && typeof answer.note === 'string' && answer.note
        ? `<div data-role="question-answer-note" style="margin-top:4px;color:#c2cfc7;white-space:pre-wrap;">${esc(answer.note)}</div>`
        : '';
      return `<div data-role="question-answer" style="margin-top:8px;font-size:12px;color:#8ee4bf;">Answered: ${selected}${note}</div>`;
    }

    function _questionHtml(n, interactive) {
      const q = n.question || {};
      const mode = String(q.response_mode || 'single_choice');
      const options = _questionOptions(q);
      const id = esc(n.notification_id);
      const note = `<textarea data-role="question-note" data-id="${id}" rows="2" placeholder="Optional note" style="width:100%;box-sizing:border-box;margin-top:8px;background:#0d1411;color:#e6f2eb;border:1px solid #24342d;border-radius:6px;padding:6px 8px;font-family:inherit;font-size:12px;resize:vertical;"></textarea>`;
      const optionRows = _questionOptionRows(q, mode, interactive);
      if (!interactive) {
        const answer = q.answer ? _questionAnswerHtml(q) : '';
        return `<div data-role="question-card" data-mode="${esc(mode)}" style="margin-top:10px;border-top:1px solid #24342d;padding-top:10px;">
          <div data-role="question-options" style="display:grid;gap:6px;">${optionRows}</div>
          ${answer}
        </div>`;
      }
      const disabled = mode === 'ack' ? '' : ' disabled';
      return `<div data-role="actions" style="margin-top:10px;display:grid;gap:8px;">
        <div data-role="question-card" data-mode="${esc(mode)}" data-id="${id}" style="display:grid;gap:8px;">
          <div data-role="question-options" style="display:grid;gap:6px;">${optionRows}</div>
          ${note}
          <button type="button" data-action="agent_question_submit" data-id="${id}"${disabled} style="justify-self:start;background:#173126;color:#8ee4bf;border:1px solid #2dd4bf;border-radius:6px;padding:5px 12px;font-family:inherit;font-size:12px;cursor:pointer;">Submit</button>
        </div>
      </div>`;
    }

    function _questionOptionRows(q, mode, interactive) {
      const rawOptions = _questionOptions(q);
      const options = mode === 'ack' && rawOptions.length === 0
        ? [{ label: 'Acknowledge', value: 'ack' }]
        : rawOptions;
      if (!interactive) {
        return options.map((option, idx) => {
          const value = _questionOptionValue(option, `option-${idx + 1}`);
          const label = _questionOptionLabel(option, value);
          return `<div data-role="question-option" style="font-size:12px;color:#d8e6dd;line-height:1.35;">${esc(label)}</div>`;
        }).join('');
      }
      const type = mode === 'multi_choice' ? 'checkbox' : 'radio';
      const name = `question-${esc((q && q.question_id) || 'unknown')}`;
      return options.map((option, idx) => {
        const value = _questionOptionValue(option, `option-${idx + 1}`);
        const label = _questionOptionLabel(option, value);
        const checked = mode === 'ack' ? ' checked' : '';
        const disabled = mode === 'ack' ? ' disabled' : '';
        return `<label data-role="question-option" style="display:flex;align-items:flex-start;gap:8px;font-size:12px;color:#d8e6dd;line-height:1.35;">
          <input type="${type}" name="${name}" data-role="question-option-input" value="${esc(value)}"${checked}${disabled} style="margin-top:2px;">
          <span>${esc(label)}</span>
        </label>`;
      }).join('');
    }

    function _actionsHtml(n) {
      const actions = Array.isArray(n.actions) ? n.actions : [];
      const id = esc(n.notification_id);
      const buttons = [];
      const btn = (a, action, label, extra) => {
        const actionId = a && a.action_id ? ` data-action-id="${esc(a.action_id)}"` : '';
        return `<button type="button" data-action="${esc(action)}" data-id="${id}"${actionId}${extra || ''} style="background:#173126;color:#8ee4bf;border:1px solid #2dd4bf;border-radius:6px;padding:5px 12px;font-family:inherit;font-size:12px;cursor:pointer;">${esc(label)}</button>`;
      };
      // Producers may override the default button text via an optional per-action
      // `label` (and `yes_label`/`no_label` for yes_no). Falls back to the
      // existing defaults so existing producers render unchanged.
      for (const a of actions) {
        const kind = a && a.kind;
        if (kind === 'ack') buttons.push(btn(a, 'ack', a.label || 'Acknowledge'));
        else if (kind === 'yes_no') { buttons.push(btn(a, 'yes_no', a.yes_label || 'Yes', ' data-choice="true"')); buttons.push(btn(a, 'yes_no', a.no_label || 'No', ' data-choice="false"')); }
        else if (kind === 'spawn_worker') buttons.push(btn(a, 'spawn_worker', a.label || 'Spawn investigator'));
        else if (kind === 'run_command') buttons.push(btn(a, 'run_command', a.label || 'Run'));
        else if (kind === 'resolved') buttons.push(btn(a, 'resolved', a.label || 'Resolve'));
      }
      if (buttons.length === 0) return '';
      return `<div data-role="actions" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">${buttons.join('')}</div>`;
    }

    function _attachActions(refs, container, ctx) {
      container.querySelectorAll('button[data-action]').forEach((el) => {
        el.addEventListener('click', () => _onAction(refs, el, ctx));
      });
      container.querySelectorAll('[data-role="question-card"]').forEach((card) => {
        card.querySelectorAll('[data-role="question-option-input"]').forEach((input) => {
          input.addEventListener('change', () => _syncQuestionSubmit(card));
        });
        _syncQuestionSubmit(card);
      });
    }

    async function _onAction(refs, el, ctx) {
      const id = el.dataset.id;
      const action = el.dataset.action;
      if (!id || !action) return;
      const card = refs.list.querySelector(`.notification-row[data-notification-id="${cssEscape(id)}"]`);
      const errorEl = card && card.querySelector('[data-role="resolve-error"]');
      if (errorEl) errorEl.style.display = 'none';

      const actionsEl = card && card.querySelector('[data-role="actions"]');
      _setActionPending(actionsEl, action);

      let options;
      let resolveAction = action;
      if (action === 'agent_question_submit') {
        options = _questionSubmitOptions(card);
        resolveAction = undefined;
        if (!options || !Array.isArray(options.selections) || options.selections.length === 0) {
          _clearActionPending(actionsEl);
          _syncQuestionSubmit(card);
          return;
        }
      } else {
        if (el.dataset.actionId || action === 'yes_no') options = {};
        if (el.dataset.actionId) options.action_id = el.dataset.actionId;
        if (action === 'yes_no') options.choice = el.dataset.choice === 'true';
      }

      const cx = ctx || refs.__ctx || {};
      const resolve = cx.actions && cx.actions.resolve;
      try {
        const reply = resolve ? await resolve(id, resolveAction, options) : { ok: false, error: 'resolve unavailable' };
        if (reply && reply.ok === false) {
          _clearActionPending(actionsEl);
          _syncQuestionSubmit(card);
          if (errorEl) { errorEl.textContent = `Resolve failed: ${reply.error || 'unknown'}`; errorEl.style.display = ''; }
          return;
        }
        if (cx.actions && typeof cx.actions.refreshNow === 'function') cx.actions.refreshNow();
      } catch (e) {
        _clearActionPending(actionsEl);
        _syncQuestionSubmit(card);
        if (errorEl) { errorEl.textContent = `Resolve failed: ${(e && e.message) || String(e)}`; errorEl.style.display = ''; }
      }
    }

    function _questionSubmitOptions(card) {
      const question = card && card.querySelector('[data-role="question-card"]');
      if (!question) return null;
      const mode = String(question.dataset.mode || '');
      const noteEl = question.querySelector('[data-role="question-note"]');
      const selections = mode === 'ack'
        ? _ackSelections(question)
        : Array.from(question.querySelectorAll('[data-role="question-option-input"]:checked')).map((input) => String(input.value || ''));
      return {
        selections: selections.filter(Boolean),
        note: noteEl ? String(noteEl.value || '') : '',
        submit: true,
      };
    }

    function _ackSelections(question) {
      const first = question.querySelector('[data-role="question-option-input"]');
      return [first && first.value ? String(first.value) : 'ack'];
    }

    function _syncQuestionSubmit(question) {
      if (!question) return;
      const submit = question.querySelector('button[data-action="agent_question_submit"]');
      if (!submit) return;
      const mode = String(question.dataset.mode || '');
      if (mode === 'ack') {
        submit.disabled = false;
        return;
      }
      submit.disabled = question.querySelectorAll('[data-role="question-option-input"]:checked').length === 0;
    }

    function _setActionPending(actionsEl, action) {
      if (!actionsEl) return;
      actionsEl.querySelectorAll('button[data-action]').forEach((button) => { button.disabled = true; });
      const label = action === 'run_command' ? 'Running…' : 'Working…';
      let pending = actionsEl.querySelector('[data-role="resolve-working"]');
      if (!pending && typeof actionsEl.insertAdjacentHTML === 'function') {
        actionsEl.insertAdjacentHTML('beforeend', `<span data-role="resolve-working" style="font-size:12px;color:#f5b78a;align-self:center;">${label}</span>`);
        pending = actionsEl.querySelector('[data-role="resolve-working"]');
      }
      if (pending) {
        pending.textContent = label;
        pending.style.display = '';
      }
    }

    function _clearActionPending(actionsEl) {
      if (!actionsEl) return;
      actionsEl.querySelectorAll('button[data-action]').forEach((button) => { button.disabled = false; });
      const pending = actionsEl.querySelector('[data-role="resolve-working"]');
      if (pending) pending.style.display = 'none';
    }

    return {
      id: 'notifications',
      name: 'Notifications',
      description: 'Operator inbox: acknowledge, answer, or spawn from agent notifications. Action buttons + the show-resolved toggle are desktop-interactive (chat_streamd writes / re-poll); the Pi renders the cards read-only.',
      color: '#f5b78a',
      manifest: {
        elements: [
          { id: 'notification-list', label: 'Notification cards' },
          { id: 'actions', label: 'Per-notification action buttons (resolve)', interactiveOnly: true },
          { id: 'show-resolved-toggle', label: 'Show-resolved toggle (re-poll)', interactiveOnly: true },
        ],
      },
      mount,
      update,
      unmount,
      _test: { TERMINAL_STATES, isTerminal, _statusHtml, _actionsHtml, _onAction },
    };
  })());


  // ── 0DTE Trading board ────────────────────────────────────────
  // SPX iron-condor pipeline: trader selector + P&L stats + last-scan + open
  // positions tables. Stateful. The trader selector is interactiveOnly (it
  // switches the data source via chat_streamd + localStorage); everything else
  // renders display-mode. Data actions are injected via ctx.actions
  // (listTraders / fetchStats) so the shared lib never touches window.cc. Ported
  // verbatim from pentacle/renderer/dashboards/0dte.js (inline styles preserved;
  // the undefined --text/--bg-elev/--font-mono tokens inherit from the surface,
  // exactly as on the desktop today).
  registerBoard((function () {
    'use strict';

    const _LS_KEY = 'pentacle.0dte.selected_trader';

    function _ls() { try { return (typeof localStorage !== 'undefined') ? localStorage : null; } catch (_) { return null; } }
    function _fmtAge(sec) {
      if (sec === null || sec === undefined) return '—';
      if (sec < 60) return `${sec.toFixed(0)}s`;
      if (sec < 3600) return `${(sec / 60).toFixed(1)}m`;
      return `${(sec / 3600).toFixed(1)}h`;
    }
    function _ageColor(sec) {
      if (sec === null || sec === undefined) return 'var(--text-dim)';
      if (sec < 30) return 'var(--green)';
      if (sec < 90) return 'var(--yellow)';
      return 'var(--red)';
    }
    function _fmtMoney(v, signed = true) {
      if (v === null || v === undefined) return '—';
      const n = Number(v);
      const sign = signed && n >= 0 ? '+' : '';
      return `${sign}$${n.toFixed(0)}`;
    }
    function _decimal(v) {
      if (v === null || v === undefined) return v;
      if (typeof v === 'number') return v;
      if (typeof v === 'string') return parseFloat(v);
      if (typeof v === 'object' && v !== null && 'N' in v) return parseFloat(v.N);
      return Number(v);
    }
    function _statusBadge(status) {
      if (!status) return { text: '—', cls: 'dim' };
      if (status === 'Filled') return { text: 'FILL', cls: 'green' };
      if (status === 'PreSubmitted' || status === 'Submitted') return { text: 'LIVE', cls: 'blue' };
      if (status === 'Cancelled' || status === 'ApiCancelled') return { text: 'CXLD', cls: 'red' };
      if (status === 'Inactive') return { text: 'INAC', cls: 'red' };
      return { text: status.slice(0, 4).toUpperCase(), cls: 'yellow' };
    }

    function mount(container, ctx) {
      if (!container) container = document.createElement('div');
      container.innerHTML = '';
      container.classList.add('zdte-shell');
      container.dataset.mode = ctx.mode;
      container.style.cssText = 'padding:24px;color:var(--text);font-family:var(--font-mono);overflow:auto;height:100%;box-sizing:border-box';

      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;gap:24px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)';
      container.appendChild(header);

      const traderInteractive = ctx.isVisible('trader-select');
      let traderSelect = null;
      if (traderInteractive) {
        const traderSelectWrap = document.createElement('div');
        traderSelectWrap.style.cssText = 'display:flex;align-items:center;gap:8px';
        const traderLabel = document.createElement('span');
        traderLabel.textContent = 'TRADER:';
        traderLabel.style.cssText = 'color:var(--text-dim);font-size:12px;letter-spacing:1px';
        traderSelect = document.createElement('select');
        traderSelect.style.cssText = 'background:var(--bg-elev);color:var(--text);border:1px solid var(--border);padding:6px 12px;font-family:var(--font-mono);font-size:14px;border-radius:4px';
        traderSelectWrap.appendChild(traderLabel);
        traderSelectWrap.appendChild(traderSelect);
        header.appendChild(traderSelectWrap);
      }

      const meta = document.createElement('div');
      meta.style.cssText = 'flex:1;display:flex;gap:24px;align-items:center;color:var(--text-dim);font-size:12px';
      header.appendChild(meta);
      const ageEl = document.createElement('span');
      ageEl.style.cssText = 'font-weight:bold;font-size:14px';
      meta.appendChild(ageEl);
      const botEl = document.createElement('span');
      meta.appendChild(botEl);
      const hostEl = document.createElement('span');
      meta.appendChild(hostEl);

      const banner = document.createElement('div');
      banner.style.cssText = 'margin-bottom:16px;display:none;padding:12px 16px;border-radius:6px;font-weight:bold';
      container.appendChild(banner);

      const statsGrid = document.createElement('div');
      statsGrid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px';
      container.appendChild(statsGrid);

      function makeStat(label) {
        const card = document.createElement('div');
        card.style.cssText = 'background:var(--bg-elev);padding:12px 16px;border-radius:6px;border:1px solid var(--border)';
        const lbl = document.createElement('div');
        lbl.textContent = label;
        lbl.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:4px';
        const val = document.createElement('div');
        val.style.cssText = 'font-size:22px;font-weight:bold';
        card.appendChild(lbl);
        card.appendChild(val);
        statsGrid.appendChild(card);
        return val;
      }

      const totalPnlVal = makeStat('TOTAL P&L');
      const unrealizedVal = makeStat('UNREALIZED');
      const realizedVal = makeStat('REALIZED');
      const filledVal = makeStat('FILLS TODAY');
      const tpVal = makeStat('TP HITS');
      const slVal = makeStat('SL HITS');
      const circuitVal = makeStat('CIRCUIT');

      const scanSection = document.createElement('div');
      scanSection.style.cssText = 'background:var(--bg-elev);padding:12px 16px;border-radius:6px;border:1px solid var(--border);margin-bottom:16px';
      const scanTitle = document.createElement('div');
      scanTitle.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:8px';
      scanSection.appendChild(scanTitle);
      const scanList = document.createElement('div');
      scanList.style.cssText = 'font-size:13px;line-height:1.6';
      scanSection.appendChild(scanList);
      container.appendChild(scanSection);

      const posTitle = document.createElement('div');
      posTitle.textContent = 'OPEN POSITIONS';
      posTitle.style.cssText = 'color:var(--text-dim);font-size:11px;letter-spacing:1px;margin-bottom:8px;margin-top:8px';
      container.appendChild(posTitle);

      const posTableWrap = document.createElement('div');
      posTableWrap.style.cssText = 'background:var(--bg-elev);border-radius:6px;border:1px solid var(--border);overflow:hidden';
      container.appendChild(posTableWrap);

      const footer = document.createElement('div');
      footer.style.cssText = 'margin-top:24px;color:var(--text-dim);font-size:11px;text-align:center';
      container.appendChild(footer);

      const refs = {
        __ctx: ctx,
        container, header, traderSelect, ageEl, botEl, hostEl, banner,
        totalPnlVal, unrealizedVal, realizedVal, filledVal, tpVal, slVal, circuitVal,
        scanTitle, scanList, posTableWrap, footer,
        selectedTrader: (_ls() && _ls().getItem(_LS_KEY)) || 'bart',
        knownTraders: [],
        lastSnapshotTs: 0,
      };

      if (traderInteractive && traderSelect) {
        refreshTraderList(refs).then(() => {
          if (refs.knownTraders.length > 0 && !refs.knownTraders.includes(refs.selectedTrader)) {
            refs.selectedTrader = refs.knownTraders[0];
            if (_ls()) _ls().setItem(_LS_KEY, refs.selectedTrader);
          }
          renderTraderOptions(refs);
        });
        traderSelect.addEventListener('change', () => {
          refs.selectedTrader = traderSelect.value;
          if (_ls()) _ls().setItem(_LS_KEY, refs.selectedTrader);
          const fetchStats = ctx.actions && ctx.actions.fetchStats;
          if (fetchStats) fetchStats(refs.selectedTrader).then((d) => update(refs, d, ctx));
        });
      }

      return refs;
    }

    async function refreshTraderList(refs) {
      const ctx = refs.__ctx || {};
      const listTraders = ctx.actions && ctx.actions.listTraders;
      if (!listTraders) return;
      try {
        const resp = await listTraders();
        if (resp && Array.isArray(resp.traders)) refs.knownTraders = resp.traders;
      } catch (e) { /* keep stale list */ }
    }

    function renderTraderOptions(refs) {
      if (!refs.traderSelect) return;
      refs.traderSelect.innerHTML = '';
      for (const t of refs.knownTraders) {
        const opt = document.createElement('option');
        opt.value = t;
        opt.textContent = t;
        if (t === refs.selectedTrader) opt.selected = true;
        refs.traderSelect.appendChild(opt);
      }
      if (refs.knownTraders.length === 0) {
        const opt = document.createElement('option');
        opt.value = refs.selectedTrader;
        opt.textContent = `${refs.selectedTrader} (no traders found)`;
        refs.traderSelect.appendChild(opt);
      }
    }

    function update(refs, resp, ctx) {
      if (!resp) return;
      if (resp._skip) return;
      const cx = refs.__ctx || ctx;

      refs._refreshCounter = (refs._refreshCounter || 0) + 1;
      if (refs._refreshCounter >= 30) {
        refs._refreshCounter = 0;
        if (refs.traderSelect) refreshTraderList(refs).then(() => renderTraderOptions(refs));
      }

      if (resp.error) {
        refs.banner.textContent = `⚠ ${resp.error}`;
        refs.banner.style.background = 'var(--red-bg, rgba(255,0,0,0.15))';
        refs.banner.style.color = 'var(--red, #f55)';
        refs.banner.style.display = '';
        refs.ageEl.textContent = '— ERROR —';
        refs.ageEl.style.color = 'var(--red)';
        return;
      }

      const snap = resp.snapshot;
      const ageSec = resp.age_sec;
      refs.ageEl.textContent = `${_fmtAge(ageSec)} ago`;
      refs.ageEl.style.color = _ageColor(ageSec);

      if (!snap) {
        refs.banner.textContent = `No snapshots found for trader_id="${resp.trader_id}". Either the bot is not running or DASHBOARD_ENABLED is false in their .env.trader.`;
        refs.banner.style.background = 'var(--bg-elev)';
        refs.banner.style.color = 'var(--text-dim)';
        refs.banner.style.display = '';
        refs.botEl.textContent = '';
        refs.hostEl.textContent = '';
        // (was refs.dayPnlVal — that ref never existed; the real field is totalPnlVal)
        refs.totalPnlVal.textContent = '—';
        refs.unrealizedVal.textContent = '—';
        refs.realizedVal.textContent = '—';
        refs.filledVal.textContent = '—';
        refs.tpVal.textContent = '—';
        refs.slVal.textContent = '—';
        refs.circuitVal.textContent = '—';
        refs.scanList.innerHTML = '';
        refs.posTableWrap.innerHTML = '';
        return;
      }

      refs.banner.style.display = 'none';
      refs.botEl.textContent = `bot=${snap.bot_version || '?'}`;
      refs.hostEl.textContent = `host=${snap.host || '?'}`;

      const circuit = snap.circuit || {};
      const todayStats = snap.today_stats || {};
      const lastScan = snap.last_scan || null;
      const positionsByAcct = snap.positions_by_account || {};

      const realized = _decimal(todayStats.realized_pnl) || 0;
      const unrealizedRaw = todayStats.unrealized_pnl;
      const unrealized = unrealizedRaw === null || unrealizedRaw === undefined ? null : _decimal(unrealizedRaw);
      const totalPnlRaw = todayStats.total_pnl;
      const totalPnl = totalPnlRaw === null || totalPnlRaw === undefined ? null : _decimal(totalPnlRaw);

      if (totalPnl !== null) {
        refs.totalPnlVal.textContent = _fmtMoney(totalPnl);
        refs.totalPnlVal.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
      } else {
        refs.totalPnlVal.textContent = _fmtMoney(realized);
        refs.totalPnlVal.style.color = realized >= 0 ? 'var(--green)' : 'var(--red)';
      }

      if (unrealized !== null) {
        refs.unrealizedVal.textContent = _fmtMoney(unrealized);
        refs.unrealizedVal.style.color = unrealized >= 0 ? 'var(--green)' : 'var(--red)';
      } else {
        refs.unrealizedVal.textContent = '—';
        refs.unrealizedVal.style.color = 'var(--text-dim)';
      }

      refs.realizedVal.textContent = _fmtMoney(realized);
      refs.realizedVal.style.color = realized >= 0 ? 'var(--green)' : 'var(--red)';

      const attempted = _decimal(todayStats.entries_attempted) || 0;
      const filled = _decimal(todayStats.entries_filled) || 0;
      refs.filledVal.textContent = `${filled}/${attempted}`;
      refs.filledVal.style.color = 'var(--text)';

      refs.tpVal.textContent = `${_decimal(todayStats.tp_hits) || 0}`;
      refs.tpVal.style.color = 'var(--green)';
      refs.slVal.textContent = `${_decimal(todayStats.sl_hits) || 0}`;
      refs.slVal.style.color = 'var(--red)';

      if (circuit.entries_blocked) {
        refs.circuitVal.textContent = 'BLOCKED'; refs.circuitVal.style.color = 'var(--red)';
      } else if (circuit.divergence_halt) {
        refs.circuitVal.textContent = 'DIVERGENT'; refs.circuitVal.style.color = 'var(--red)';
      } else if (circuit.vix_breached) {
        refs.circuitVal.textContent = 'VIX HALT'; refs.circuitVal.style.color = 'var(--red)';
      } else {
        refs.circuitVal.textContent = 'OK'; refs.circuitVal.style.color = 'var(--green)';
      }

      if (circuit.divergence_halt || circuit.vix_breached) {
        const flags = [];
        if (circuit.divergence_halt) flags.push('DIVERGENCE HALT — entries blocked until operator clears ~/.0dte/control/clear_divergence');
        if (circuit.vix_breached) flags.push('VIX CIRCUIT BREAKER — VIX ≥ 26 today');
        refs.banner.textContent = '⚠ ' + flags.join(' · ');
        refs.banner.style.background = 'rgba(255, 100, 100, 0.15)';
        refs.banner.style.color = 'var(--red, #f55)';
        refs.banner.style.display = '';
      }

      if (lastScan && lastScan.decision_dt) {
        const decTs = String(lastScan.decision_dt).slice(-8);
        refs.scanTitle.textContent = `LAST SCAN @ ${decTs} • scored=${_decimal(lastScan.scored_count) || 0} candidates=${_decimal(lastScan.candidates_count) || 0}`;
        refs.scanList.innerHTML = '';
        const picks = Array.isArray(lastScan.top_picks) ? lastScan.top_picks : [];
        for (const pick of picks.slice(0, 5)) {
          const row = document.createElement('div');
          const cal = (_decimal(pick.best_tp_cal_prob) || 0) * 100;
          const ev = _decimal(pick.best_ev) || 0;
          row.innerHTML = `→ <span style="color:var(--blue)">${pick.strategy || '?'}</span> tp${_decimal(pick.best_tp) || 0} cal=${cal.toFixed(1)}% EV=$${ev.toFixed(2)}`;
          refs.scanList.appendChild(row);
        }
        if (picks.length === 0) {
          refs.scanList.innerHTML = '<span style="color:var(--text-dim)">no candidates passed cutoffs</span>';
        }
      } else {
        refs.scanTitle.textContent = 'LAST SCAN — none yet';
        refs.scanList.innerHTML = '';
      }

      refs.posTableWrap.innerHTML = '';
      const acctNames = Object.keys(positionsByAcct);
      if (acctNames.length === 0) {
        refs.posTableWrap.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-dim)">no accounts</div>';
      }
      for (const acct of acctNames) {
        const positions = positionsByAcct[acct] || [];
        const open = positions.filter(p => !p.closed);
        const closed = positions.filter(p => p.closed);
        const closedPnl = closed.reduce((s, p) => s + (_decimal(p.close_pnl) || 0), 0);

        const acctHeader = document.createElement('div');
        acctHeader.style.cssText = 'padding:8px 16px;background:rgba(255,255,255,0.03);border-bottom:1px solid var(--border);display:flex;gap:24px;font-size:12px;color:var(--text-dim)';
        acctHeader.innerHTML = `
          <span style="color:var(--text);font-weight:bold">[${acct}]</span>
          <span>open=${open.length}</span>
          <span>closed=${closed.length}</span>
          <span>realized=<span style="color:${closedPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${_fmtMoney(closedPnl)}</span></span>
        `;
        refs.posTableWrap.appendChild(acctHeader);

        if (open.length === 0) {
          const empty = document.createElement('div');
          empty.style.cssText = 'padding:16px;text-align:center;color:var(--text-dim);font-size:12px';
          empty.textContent = 'no open positions';
          refs.posTableWrap.appendChild(empty);
          continue;
        }

        const table = document.createElement('table');
        table.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px';
        const thead = document.createElement('thead');
        thead.innerHTML = `
          <tr style="color:var(--text-dim);text-align:left">
            <th style="padding:8px 16px;font-weight:normal">STRATEGY</th>
            <th style="padding:8px;font-weight:normal">QTY</th>
            <th style="padding:8px;font-weight:normal">CREDIT</th>
            <th style="padding:8px;font-weight:normal">UNREAL</th>
            <th style="padding:8px;font-weight:normal">STRIKES</th>
            <th style="padding:8px;font-weight:normal">OCA</th>
            <th style="padding:8px;font-weight:normal">TP</th>
            <th style="padding:8px;font-weight:normal">SL</th>
            <th style="padding:8px 16px;font-weight:normal">ENTRY</th>
          </tr>
        `;
        table.appendChild(thead);
        const tbody = document.createElement('tbody');
        for (const p of open) {
          const tr = document.createElement('tr');
          tr.style.cssText = 'border-top:1px solid var(--border)';
          const strikes = p.strikes || {};
          const lp = _decimal(strikes.lp);
          const sp = _decimal(strikes.sp);
          const sc = _decimal(strikes.sc);
          const lc = _decimal(strikes.lc);
          const credit = _decimal(p.credit) || 0;
          const oca = String(p.oca_group || '').slice(-12);
          const tp = _statusBadge(p.tp_status);
          const sl = _statusBadge(p.sl_status);
          const entryTime = String(p.entry_time || '').slice(-8);
          const unrealRaw = p.unrealized_pnl;
          let unrealCell;
          if (unrealRaw === null || unrealRaw === undefined) {
            unrealCell = `<td style="padding:8px;color:var(--text-dim)">—</td>`;
          } else {
            const unr = _decimal(unrealRaw);
            const color = unr >= 0 ? 'var(--green)' : 'var(--red)';
            const sign = unr >= 0 ? '+' : '';
            unrealCell = `<td style="padding:8px;color:${color};font-weight:bold">${sign}$${unr.toFixed(0)}</td>`;
          }
          tr.innerHTML = `
            <td style="padding:8px 16px;color:var(--blue)">${p.strategy || '?'}</td>
            <td style="padding:8px">${_decimal(p.quantity) || 0}</td>
            <td style="padding:8px">$${credit.toFixed(2)}</td>
            ${unrealCell}
            <td style="padding:8px;color:var(--text-dim);font-size:11px">${lp}/${sp}P · ${sc}/${lc}C</td>
            <td style="padding:8px;color:var(--text-dim);font-size:11px">${oca}</td>
            <td style="padding:8px;color:var(--${tp.cls})">${tp.text}</td>
            <td style="padding:8px;color:var(--${sl.cls})">${sl.text}</td>
            <td style="padding:8px 16px;color:var(--text-dim);font-size:11px">${entryTime}</td>
          `;
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        refs.posTableWrap.appendChild(table);
      }

      const accountsStr = Array.isArray(snap.trader_accounts) ? snap.trader_accounts.join(', ') : '?';
      refs.footer.textContent = `accounts=${accountsStr}  •  table=0dte-snapshots  •  trader_id=${resp.trader_id}  •  refreshes every 5s`;
    }

    function unmount(_refs) {}

    return {
      id: '0dte-trading',
      name: '0DTE Trading',
      description: 'SPX iron condor pipeline — multi-trader live snapshots. The trader selector is desktop-interactive (switches data source); the Pi renders the selected trader display-only.',
      color: '#3fb950',
      manifest: {
        elements: [
          { id: 'stats', label: 'P&L stats grid' },
          { id: 'last-scan', label: 'Last scan summary' },
          { id: 'positions', label: 'Open positions tables' },
          { id: 'trader-select', label: 'Trader selector (switches data source)', interactiveOnly: true },
        ],
      },
      mount,
      update,
      unmount,
      _test: { _fmtAge, _fmtMoney, _decimal, _statusBadge },
    };
  })());


  return {
    VERSION,
    DEFAULT_STATUSES,
    TOKENS,
    MODES,
    helpers: { DEFAULT_STATUSES, statusesOrDefault, selectVisibleStatuses, partitionRows, sortForList, escapeHtml },
    statusesOrDefault,
    selectVisibleStatuses,
    partitionRows,
    sortForList,
    escapeHtml,
    elementVisibleInMode,
    manifestElement,
    visibleElements,
    defineBoard,
    registerBoard,
    renderBoard,
    makeCtx,
    mountBoard,
    updateBoard,
    unmountBoard,
    injectBoardCss,
    boards,
  };
});
