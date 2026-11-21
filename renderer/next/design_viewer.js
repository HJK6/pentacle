'use strict';

const { getDesignTarget } = require('./design_board');
const { designMediaUrl, mountDesignCanvas, pointerIcon } = require('./design_canvas');

const catalogInterval = 10_000;
const seenKey = 'pentacle.next.design.seen';
const fullIcon = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 4H4v5m11-5h5v5m-5 11h5v-5M4 15v5h5"/></svg>';
const backIcon = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m10 3-5 5 5 5"/></svg>';
const closeIcon = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';

function relativeTime(value, now) {
  const minutes = Math.max(0, Math.floor((now - Date.parse(value)) / 60_000));
  if (!Number.isFinite(minutes)) return '';
  if (minutes === 0) return 'just now';
  if (minutes < 60) return minutes + 'm ago';
  if (minutes < 1440) return Math.floor(minutes / 60) + 'h ago';
  return Math.floor(minutes / 1440) + 'd ago';
}

function createDesignViewer(root, options = {}) {
  const document = root.ownerDocument;
  const window = document.defaultView;
  const fetch = options.fetch ?? window.fetch.bind(window);
  const phone = () => !!options.embedded || window.innerWidth < 600;
  let lifetime, request, timer, observer;
  let catalog, etag, failed, notice, requestedTarget, active, canvas;
  let projectFilter = null, query = '', seen = {};
  let pullStart = null, pulled = 0, menuOpen = false;

  function add(parent, tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    parent.append(element);
    return element;
  }

  function control(parent, label, className, content, onClick, icon = false) {
    const element = add(parent, 'button', className);
    element.type = 'button';
    if (icon) element.innerHTML = content;
    else element.textContent = content;
    if (icon || label !== content) element.setAttribute('aria-label', label);
    element.title = label;
    element.addEventListener('click', onClick);
    return element;
  }

  root.classList.add('design-viewer');
  const bar = add(root, 'header', 'design-bar');
  const homeBar = add(bar, 'div', 'design-home-bar');
  const title = control(homeBar, 'Design', 'design-title', 'Design', () => setMenu(!menuOpen));
  title.setAttribute('aria-expanded', 'false');
  const tabs = add(homeBar, 'nav', 'design-tabs');
  tabs.setAttribute('aria-label', 'Projects');
  const search = add(homeBar, 'input', 'design-search');
  search.type = 'search';
  search.placeholder = 'Search';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', 'Search designs');
  search.addEventListener('input', () => { query = search.value; renderHome(); });
  const menu = add(homeBar, 'div', 'design-menu');
  menu.hidden = true;
  control(menu, 'Reload', '', 'Reload', () => { setMenu(false); void refresh(); });
  const boardBar = add(bar, 'div', 'design-board-bar');
  control(boardBar, 'Back', 'design-icon', backIcon, () => goHome(), true);
  const breadcrumb = control(boardBar, 'Project', 'design-project-crumb', '', () => {
    const project = active.project.id;
    goHome(project);
  });
  add(boardBar, 'span', 'design-crumb-divider', '/').setAttribute('aria-hidden', 'true');
  const boardTitle = add(boardBar, 'strong', 'design-board-title');
  if (!options.embedded) {
    const actions = add(bar, 'div', 'design-card-actions');
    if (options.onFullScreen) control(actions, 'Full screen', 'design-icon design-full', fullIcon, options.onFullScreen, true);
    if (options.onClose) control(actions, 'Close', 'design-icon', closeIcon, options.onClose, true);
  }
  const home = add(root, 'div', 'design-home');
  const pull = add(home, 'div', 'design-pull');
  pull.setAttribute('aria-hidden', 'true');
  const homeContents = add(home, 'div', 'design-home-contents');
  const board = add(root, 'div', 'design-board');
  const drawing = add(board, 'div', 'design-drawing');
  const update = add(board, 'div', 'design-update design-pill');
  update.hidden = true;
  add(update, 'span', '', 'New version');
  control(update, 'Show new version', '', 'Show', showUpdate);

  function setMenu(open) {
    menuOpen = open;
    title.setAttribute('aria-expanded', String(open));
    menu.hidden = !open;
  }

  function readSeen() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(seenKey));
      if (stored && typeof stored === 'object') return stored;
    } catch {}
    return {};
  }

  function markSeen(project, design) {
    seen[project.id + '/' + design.id] = design.changed_at;
    try { window.localStorage.setItem(seenKey, JSON.stringify(seen)); } catch {}
  }

  function renderTabs() {
    tabs.replaceChildren();
    if (projectFilter && !catalog?.projects.some(project => project.id === projectFilter)) projectFilter = null;
    for (const [id, label] of [[null, 'All'], ...(catalog?.projects ?? []).map(project => [project.id, project.title])]) {
      const tab = control(tabs, label, '', label, () => { projectFilter = id; renderTabs(); renderHome(); });
      tab.setAttribute('aria-pressed', String(id === projectFilter));
    }
  }

  function renderTile(parent, project, design, views) {
    const tile = control(parent, design.title, 'design-tile', '', () => openDesign(project, design));
    const pictures = add(tile, 'div', 'design-tile-pictures');
    const screens = design.nodes.filter(node => node.type === 'screen');
    const first = screens[0];
    const phoneScreen = screen => screen.w < screen.h;
    const selected = first && phoneScreen(first) ? screens.filter(phoneScreen).slice(0, 3) : screens.slice(0, 1);
    pictures.classList.toggle('is-phone', selected.length > 0 && phoneScreen(selected[0]));
    for (const screen of selected) {
      const url = designMediaUrl(project, views.get(screen.view));
      if (!url) continue;
      const image = add(pictures, 'img', 'design-tile-still');
      image.alt = '';
      image.src = url;
      image.loading = 'lazy';
      image.decoding = 'async';
    }
    if (screens.some(screen => views.get(screen.view)?.variant === 'prototype')) {
      const badge = add(pictures, 'span', 'design-tile-prototype');
      badge.setAttribute('aria-label', 'Prototype');
      badge.innerHTML = pointerIcon;
    }
    const name = add(tile, 'span', 'design-tile-name');
    if (design.changed_at && seen[project.id + '/' + design.id] !== design.changed_at) {
      add(name, 'span', 'design-unread').setAttribute('aria-label', 'New or changed');
    }
    add(name, 'span', '', design.title);
    const changeAge = relativeTime(design.changed_at, Date.now());
    add(tile, 'span', 'design-tile-meta', screens.length + (screens.length === 1 ? ' screen' : ' screens') +
      (changeAge ? ' · ' + changeAge : ''));
  }

  function renderHome() {
    const scroll = home.scrollTop;
    homeContents.replaceChildren();
    if (notice) add(homeContents, 'p', 'design-notice', notice).setAttribute('role', 'status');
    if (!catalog) {
      const message = add(homeContents, 'div', 'design-empty');
      add(message, 'p', '', failed ? 'Design did not load' : 'Loading…').setAttribute('role', 'status');
      if (failed) control(message, 'Try again', '', 'Try again', () => { failed = false; renderHome(); void refresh(); });
      return;
    }
    const term = query.trim().toLowerCase();
    let matches = 0;
    for (const project of catalog.projects) {
      if (projectFilter && project.id !== projectFilter) continue;
      const designs = project.designs
        .filter(design => (design.title + ' ' + project.title).toLowerCase().includes(term))
        .sort((left, right) => Date.parse(right.changed_at) - Date.parse(left.changed_at));
      if (!designs.length) continue;
      matches += designs.length;
      if (!projectFilter) add(homeContents, 'h2', 'design-project-title', project.title);
      const grid = add(homeContents, 'div', 'design-grid');
      const views = new Map((project.manifest?.views ?? []).map(view => [view.id, view]));
      for (const design of designs) renderTile(grid, project, design, views);
    }
    if (!matches) add(homeContents, 'p', 'design-empty', term ? 'Nothing matches “' + query.trim() + '”.' : 'No designs yet.');
    home.scrollTop = scroll;
  }

  function renderMode() {
    homeBar.hidden = home.hidden = !!active;
    boardBar.hidden = board.hidden = !active;
    if (active) {
      breadcrumb.textContent = active.project.title;
      breadcrumb.setAttribute('aria-label', active.project.title);
      breadcrumb.title = active.project.title;
      boardTitle.textContent = active.design.title;
    }
  }

  function navigate(current) {
    const target = active ? { project_id: active.project.id, design_id: active.design.id } : null;
    if (target && current) target.view_id = current;
    options.onNavigate?.(target);
  }

  function openDesign(project, design, screen, previous = null, focus = true) {
    notice = '';
    requestedTarget = undefined;
    active = { project, design };
    markSeen(project, design);
    canvas?.destroy();
    setMenu(false);
    update.hidden = true;
    renderMode();
    canvas = mountDesignCanvas(drawing, {
      project, design, phone, fetch,
      screenId: screen?.view,
      camera: previous?.camera,
      overview: previous?.overview,
      onBack: () => goHome(),
      onScreenChange: navigate,
    });
    // A single-screen board opens with its screen current, but its board
    // address stays canonical until the reader selects that screen.
    navigate(screen?.view);
    if (focus) canvas.focus();
  }

  function goHome(project = null, missing = false) {
    if (active) {
      canvas?.destroy();
      canvas = null;
      home.scrollTop = 0;
    }
    active = null;
    requestedTarget = undefined;
    projectFilter = project;
    notice = missing ? 'This design is no longer available.' : '';
    renderMode();
    renderTabs();
    renderHome();
    navigate();
  }

  function pendingUpdate() {
    if (!active || !catalog) return null;
    const { project, design } = getDesignTarget(catalog.projects, {
      project_id: active.project.id, design_id: active.design.id,
    });
    if (!design) {
      const latest = catalog.projects.find(project => project.id === active.project.id);
      if (latest && !latest.revision_id) return null;
      return { missing: true };
    }
    return design.changed_at !== active.design.changed_at ? { project, design } : null;
  }

  function showUpdate() {
    const pending = pendingUpdate();
    if (!pending) return;
    if (pending.missing) { goHome(null, true); return; }
    const snapshot = canvas.snapshot();
    const screen = pending.design.nodes.find(node => node.type === 'screen' && node.view === snapshot.current);
    openDesign(pending.project, pending.design, screen, snapshot);
  }

  async function refresh() {
    if (!lifetime || request) return;
    const pending = new window.AbortController();
    request = pending;
    try {
      const response = await fetch('/api/designs', {
        headers: etag ? { 'If-None-Match': etag } : {}, signal: pending.signal,
      });
      if (pending.signal.aborted || response.status === 304) return;
      if (!response.ok) throw new Error('Design catalog unavailable');
      const next = await response.json();
      if (pending.signal.aborted) return;
      if (next?.schema !== 'design-catalog.v1' || !Array.isArray(next.projects)) {
        throw new Error('Invalid Design catalog');
      }
      catalog = next;
      etag = response.headers.get('ETag');
      failed = false;
      if (requestedTarget) {
        const found = getDesignTarget(catalog.projects, requestedTarget);
        if (found.design) openDesign(found.project, found.design, found.screen);
        else goHome(null, true);
      } else if (active) {
        update.hidden = !pendingUpdate();
      } else {
        renderTabs();
        renderHome();
      }
    } catch (error) {
      if (pending.signal.aborted || catalog) return;
      failed = true;
      renderHome();
    } finally {
      if (request === pending) request = null;
    }
  }

  function setPull(amount) {
    pulled = amount;
    pull.style.height = amount + 'px';
    pull.classList.toggle('ready', amount >= 56);
  }

  function size() {
    root.classList.toggle('design-phone', phone());
    search.hidden = root.clientWidth < 860;
    canvas?.resize();
  }

  function handleKey(event) {
    if (!lifetime || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey ||
        event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return false;
    if (event.key === 'Escape' && menuOpen) { setMenu(false); title.focus(); return true; }
    return canvas?.handleKey(event.key) ?? false;
  }

  function open(target = null) {
    close();
    lifetime = new window.AbortController();
    const signal = lifetime.signal;
    root.dataset.designView = options.embedded ? 'embed' : 'workspace';
    requestedTarget = target;
    seen = readSeen();
    search.value = '';
    renderMode();
    renderTabs();
    renderHome();
    size();
    if (window.ResizeObserver) {
      observer = new window.ResizeObserver(size);
      observer.observe(root);
    }
    window.addEventListener('resize', size, { signal });
    document.addEventListener('click', event => {
      if (menuOpen && !menu.contains(event.target) && !title.contains(event.target)) setMenu(false);
    }, { signal });
    home.addEventListener('touchstart', event => {
      pullStart = phone() && home.scrollTop <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
    }, { signal, passive: true });
    home.addEventListener('touchmove', event => {
      if (pullStart === null || event.touches.length !== 1) return;
      const distance = event.touches[0].clientY - pullStart;
      if (distance <= 0) { setPull(0); return; }
      if (event.cancelable) event.preventDefault();
      setPull(Math.min(76, distance / 2));
    }, { signal, passive: false });
    home.addEventListener('touchend', () => {
      pullStart = null;
      const ready = pulled >= 56;
      setPull(0);
      if (ready) void refresh();
    }, { signal });
    home.addEventListener('touchcancel', () => { pullStart = null; setPull(0); }, { signal });
    timer = window.setInterval(refresh, catalogInterval);
    if (!target) navigate();
    return refresh();
  }

  function close() {
    lifetime?.abort();
    lifetime = null;
    request?.abort();
    request = null;
    window.clearInterval(timer);
    observer?.disconnect();
    observer = null;
    canvas?.destroy();
    canvas = null;
    active = catalog = etag = requestedTarget = null;
    failed = false;
    notice = query = '';
    projectFilter = pullStart = null;
    home.scrollTop = 0;
    setMenu(false);
    setPull(0);
  }

  return { open, close, refresh, handleKey };
}

module.exports = { createDesignViewer };
