(function() {

function _escape(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _fmtTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function _fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n > 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function _unique(values) {
  return Array.from(new Set(values.filter(Boolean))).sort((a, b) => String(a).localeCompare(String(b)));
}

function _artifactKey(item) {
  return item && (item.artifactKey || item.id || item.entryUrl || item.url);
}

function _matchesFilters(item, filters) {
  const text = [item.title, item.repo, item.machine, item.summary, item.screen, item.artifactKey, ...(Array.isArray(item.tags) ? item.tags : [])]
    .join(' ')
    .toLowerCase();
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

function _renderFilterControls(refs, artifacts) {
  const repos = _unique(artifacts.map((item) => item.repo));
  const machines = _unique(artifacts.map((item) => item.machine));
  const tags = _unique(artifacts.flatMap((item) => Array.isArray(item.tags) ? item.tags : []));
  refs.filtersEl.innerHTML = `
    <input data-filter="query" value="${_escape(refs.filters.query)}" placeholder="Search reviews">
    <select data-filter="repo">${_renderOptions(repos, refs.filters.repo, 'All repos')}</select>
    <select data-filter="machine">${_renderOptions(machines, refs.filters.machine, 'All machines')}</select>
    <select data-filter="tag">${_renderOptions(tags, refs.filters.tag, 'All tags')}</select>`;
  refs.filtersEl.querySelectorAll('[data-filter]').forEach((el) => {
    el.addEventListener('input', () => {
      refs.filters[el.dataset.filter] = el.value;
      update(refs, refs.lastData);
    });
    el.addEventListener('change', () => {
      refs.filters[el.dataset.filter] = el.value;
      update(refs, refs.lastData);
    });
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
  ].map((card) => `<div class="ui-review-stat">
    <span>${_escape(card.label)}</span>
    <b>${_escape(card.value)}</b>
  </div>`).join('');
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

function mount(container) {
  container.innerHTML = '';
  const root = document.createElement('div');
  root.className = 'ui-review-dashboard';
  root.innerHTML = `
    <style>
      .ui-review-dashboard { height:100%; min-height:0; display:flex; flex-direction:column; gap:14px; padding:16px; color:#dce8e1; background:linear-gradient(180deg,#0d1511,#0a100d); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif; }
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
      @media (max-width: 980px) { .ui-review-layout { grid-template-columns:1fr; } .ui-review-stats, .ui-review-filters { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    </style>
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
  container.appendChild(root);
  return {
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

function update(refs, data) {
  if (!refs || !data) return;
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
  _renderFilterControls(refs, artifacts);
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
      update(refs, data);
    });
  });
  _selectArtifact(refs, visibleArtifacts.find((item) => _artifactKey(item) === refs.selectedId) || null);
}

function unmount(_refs) {}

window.DASHBOARDS.push({
  id: 'ui-review',
  name: 'UI Review',
  description: 'Visual QA artifacts from repos on this machine',
  color: '#7ef0ba',
  mount,
  update,
  unmount,
  pollFn: () => window.cc.listUiReviewArtifacts(),
  pollInterval: 5000,
  idlePollInterval: 15000,
  idleFn: () => true,
});

})();
