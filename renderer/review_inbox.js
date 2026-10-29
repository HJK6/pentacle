'use strict';

const STORAGE_KEY = 'pentacle.reviewInbox.v1';
const REQUEST_DEADLINE_MS = 15000;
const RECEIPT_RETRY_WINDOW_MS = 23 * 60 * 60 * 1000;
const PROJECTS = Object.freeze(['Agenteach', 'DUMBO', 'Pentacle', 'Personal', 'Other']);
const projectName = value => PROJECTS.includes(value) ? value : 'Other';
function safeLink(value) {
  if (typeof value !== 'string' || value.length > 2048) return null;
  try { const url = new URL(value); return url.protocol === 'https:' && url.username === '' && url.password === '' ? url.href : null; }
  catch { return null; }
}
function exactPaintedMessage(doc, id) {
  if (!id) return null;
  const matches = [];
  for (const root of doc.querySelectorAll('.slot-chat-list[data-stream-id]')) {
    if (!root.offsetParent) continue;
    for (const row of root.querySelectorAll('[data-transcript-key]')) {
      let key;
      try { key = JSON.parse(row.dataset.transcriptKey); } catch { continue; }
      if (!Array.isArray(key) || key.length !== 2 || key[0] !== root.dataset.streamId) continue;
      if (key[1] === id || [...row.querySelectorAll('[data-reply-message-id]')].some(b => b.dataset.replyMessageId === id)) matches.push(row);
    }
  }
  return matches.length === 1 ? matches[0] : null;
}
function createReviewInbox({ onLayout = () => {}, now = () => Date.now(), requestDeadlineMs = REQUEST_DEADLINE_MS } = {}) {
  const doc = typeof document !== 'undefined' ? document : typeof window !== 'undefined' ? window.document : null;
  const main = doc?.querySelector('.main');
  const rail = doc?.getElementById('review-inbox');
  if (!rail || !main) return { refresh() {}, close() {} };
  const $ = id => doc.getElementById(id);
  const titleToggle = $('review-inbox-title-toggle'), phoneToggle = $('review-inbox-phone-toggle');
  const edge = $('review-inbox-edge'), closeButton = $('review-inbox-close'), scrim = $('review-inbox-scrim');
  const banner = $('review-inbox-banner');
  let collapsed = false;
  try { collapsed = localStorage.getItem(STORAGE_KEY) === 'collapsed'; } catch {}
  let mode = '', open = false, lastGood = null, latest = null, loading = false, busyId = null, notice = '';
  let lastCount = null;
  const text = (tag, value, cls) => { const el = doc.createElement(tag); el.textContent = value; if (cls) el.className = cls; return el; };
  function setNotice(value) { notice = value; render(); }
  function setMode() {
    const width = main.clientWidth;
    const next = width < 840 || doc.body.classList.contains('web-sidebar-narrow') ? 'phone' : width < 1180 ? 'overlay' : 'desktop';
    if (next !== mode) {
      mode = next; open = false;
      if (mode === 'desktop') {
        try { collapsed = localStorage.getItem(STORAGE_KEY) === 'collapsed'; } catch { collapsed = false; }
      }
    }
    if (mode === 'overlay' && !open) collapsed = true;
    main.classList.toggle('review-inbox-expanded', mode === 'desktop' && !collapsed);
    main.classList.toggle('review-inbox-collapsed', mode !== 'phone' && (collapsed || mode === 'overlay' && !open));
    main.classList.toggle('review-inbox-overlay-open', mode !== 'desktop' && open);
    main.classList.toggle('review-inbox-phone', mode === 'phone');
    rail.setAttribute('aria-modal', String(mode !== 'desktop' && open));
    titleToggle.hidden = mode === 'phone';
    phoneToggle.hidden = mode !== 'phone';
    scrim.hidden = !(mode !== 'desktop' && open);
    titleToggle.setAttribute('aria-expanded', String(mode === 'desktop' ? !collapsed : open));
    phoneToggle.setAttribute('aria-expanded', String(open));
    edge.setAttribute('aria-expanded', String(mode === 'desktop' ? !collapsed : open));
    edge.setAttribute('aria-label', mode === 'desktop' && !collapsed ? 'Collapse review inbox' : 'Open review inbox');
    onLayout();
  }
  function show() {
    if (mode === 'desktop') collapsed = false;
    else { open = true; doc.body.classList.remove('sidebar-open'); $('sidebar-toggle')?.setAttribute('aria-expanded', 'false'); }
    setMode();
    if (mode !== 'desktop') closeButton.focus({ preventScroll: true });
  }
  function close() {
    if (mode === 'desktop') {
      collapsed = true;
      try { localStorage.setItem(STORAGE_KEY, 'collapsed'); } catch {}
    } else open = false;
    setMode();
    (mode === 'phone' ? phoneToggle : mode === 'overlay' ? edge : titleToggle)?.focus({ preventScroll: true });
  }
  function toggle() {
    setMode();
    if (mode === 'desktop') {
      collapsed = !collapsed;
      try { localStorage.setItem(STORAGE_KEY, collapsed ? 'collapsed' : 'expanded'); } catch {}
      setMode();
    } else if (open) close(); else show();
  }
  titleToggle.addEventListener('click', toggle);
  phoneToggle.addEventListener('click', toggle);
  edge.addEventListener('click', toggle);
  closeButton.addEventListener('click', close);
  scrim.addEventListener('click', close);
  $('sidebar-toggle')?.addEventListener('click', () => { if (mode === 'phone' && open) close(); }, true);
  doc.addEventListener('keydown', event => {
    if (event.key === 'Escape' && mode !== 'desktop' && open) { event.preventDefault(); close(); return; }
    if (event.key !== 'Tab' || mode === 'desktop' || !open) return;
    const focusables = [...rail.querySelectorAll('button:not([disabled]),a[href],summary')].filter(el => el.offsetParent);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables.at(-1);
    if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  const resize = window.ResizeObserver ? new ResizeObserver(() => setMode()) : null;
  resize?.observe(main);
  setMode();

  function card(item, disabled) {
    const article = doc.createElement('article'); article.className = 'review-inbox-card'; article.dataset.reviewId = item.id;
    if (item.state !== 'pending') article.append(text('span', projectName(item.project), 'review-inbox-project-label'));
    article.append(text('span', { review: 'Review', answer: 'Answer', input: 'Needs input' }[item.kind] || item.kind, 'review-inbox-kind'));
    article.append(text('h5', item.title));
    if (item.first_raised_at) article.append(text('p', `First raised ${new Date(item.first_raised_at).toLocaleString()}`, 'review-inbox-meta'));
    else article.append(text('p', item.origin_note || 'Source not linked', 'review-inbox-meta'));
    if (item.state === 'done' && item.acked_at) article.append(text('p', `Received ${new Date(item.acked_at).toLocaleString()}`, 'review-inbox-meta'));
    if (item.state === 'resolved') article.append(text('p', `Resolved by Lews ${new Date(item.resolved_at).toLocaleString()}: ${item.resolution_reason}`, 'review-inbox-meta'));
    const actions = doc.createElement('div'); actions.className = 'review-inbox-actions';
    if (item.state !== 'resolved') {
      if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {
        const action = text('button', item.state === 'pending' ? 'Got it' : 'Undo'); action.type = 'button';
        action.setAttribute('aria-label', `${item.state === 'pending' ? 'Got it' : 'Undo'}: ${item.title}`);
        action.disabled = disabled || busyId === item.id;
        action.addEventListener('click', () => write(item, item.state === 'pending' ? 'ack' : 'unack'));
        actions.append(action);
      } else actions.append(text('span', 'Read only on desktop', 'review-inbox-meta'));
    }
    if (item.link !== undefined) {
      const url = safeLink(item.link);
      if (url) { const link = text('a', 'Open link'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; actions.append(link); }
      else actions.append(text('span', 'Link unavailable', 'review-inbox-meta'));
    }
    const showChat = text('button', 'Show chat'); showChat.type = 'button';
    const messageId = item.source?.reply_to_message_id;
    showChat.disabled = !messageId;
    showChat.addEventListener('click', () => {
      const row = exactPaintedMessage(doc, messageId);
      if (!row) { setNotice('Chat message not open here'); return; }
      row.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
      row.tabIndex = -1; row.focus({ preventScroll: true }); row.classList.add('review-inbox-highlight');
      setTimeout(() => row.classList.remove('review-inbox-highlight'), 2200);
      setNotice('');
    });
    actions.append(showChat); article.append(actions);
    return article;
  }
  function render() {
    const focusedCard = rail.contains(doc.activeElement) ? doc.activeElement.closest('[data-review-id]') : null;
    const focusedId = focusedCard?.dataset.reviewId;
    const focusedAction = doc.activeElement?.textContent;
    const focusedHeadingProject = rail.contains(doc.activeElement)
      ? doc.activeElement.closest('.review-inbox-project-card')?.dataset.project : null;
    const feed = latest?.state === 'ok' ? latest : lastGood;
    const disabled = latest?.state !== 'ok';
    const items = feed?.items || [];
    const focusedProject = projectName(items.find(x => x.id === focusedId)?.project);
    const pending = items.filter(x => x.state === 'pending'), done = items.filter(x => x.state === 'done'), resolved = items.filter(x => x.state === 'resolved');
    const count = pending.length;
    if (count !== lastCount) { $('review-inbox-count').textContent = String(count); lastCount = count; }
    $('review-inbox-edge-count').textContent = String(count);
    phoneToggle.textContent = `To do (${count})`;
    const status = latest?.state === 'missing' ? 'Inbox not initialized yet'
      : latest?.state === 'invalid' ? 'Inbox data is invalid. Ask Lews to check the feed.'
      : latest?.state === 'unavailable' ? 'Inbox unavailable. Try again.'
      : latest?.state === 'unreachable' ? `Can't reach Pentacle. Showing items last loaded at ${lastGood ? new Date(lastGood.read_at).toLocaleString() : 'unknown time'}.`
      : !latest ? 'Loading review inbox…' : '';
    const messages = [notice, status];
    if (latest?.state === 'ok') {
      if (latest.stale) messages.push(`Inbox may be out of date — Lews last updated ${new Date(latest.updated_at).toLocaleString()}`);
      if (latest.warning) messages.push(latest.warning);
    }
    banner.replaceChildren(...messages.filter(Boolean).map(message => text('div', message, 'review-inbox-banner-line')));
    banner.hidden = !banner.childElementCount;
    rail.classList.toggle('review-inbox-disabled', disabled);
    const pendingRoot = $('review-inbox-pending'); pendingRoot.replaceChildren();
    if (latest?.state === 'ok' && !pending.length) pendingRoot.append(text('p', 'Nothing waiting on you', 'review-inbox-empty'));
    for (const project of PROJECTS) {
      const members = pending.filter(item => projectName(item.project) === project);
      if (!members.length) continue;
      const group = doc.createElement('section'); group.className = 'review-inbox-project-card'; group.dataset.project = project;
      const heading = text('h4', `${project} (${members.length})`, 'review-inbox-project-heading');
      heading.tabIndex = -1; heading.setAttribute('aria-label', `${project}, ${members.length} waiting`);
      group.append(heading, ...members.map(item => card(item, disabled)));
      pendingRoot.append(group);
    }
    const doneRoot = $('review-inbox-done-list'); doneRoot.replaceChildren(...done.map(x => card(x, disabled)));
    const resolvedRoot = $('review-inbox-resolved-list'); resolvedRoot.replaceChildren(...resolved.map(x => card(x, true)));
    $('review-inbox-done').querySelector('summary').textContent = `Done (${done.length})`;
    $('review-inbox-resolved').querySelector('summary').textContent = `Resolved (${resolved.length})`;
    if (focusedId) {
      const same = [...rail.querySelectorAll('[data-review-id]')].find(x => x.dataset.reviewId === focusedId && x.offsetParent);
      const next = same && [...same.querySelectorAll('button,a')].find(x => x.textContent === focusedAction && !x.disabled);
      const groupIndex = PROJECTS.indexOf(focusedProject);
      const groups = [...pendingRoot.querySelectorAll('.review-inbox-project-card')];
      const sameGroup = groups.find(group => group.dataset.project === focusedProject);
      const nextGroup = groups.find(group => PROJECTS.indexOf(group.dataset.project) > groupIndex);
      const fallback = [...(sameGroup?.querySelectorAll('button') || [])].find(x => !x.disabled)
        || nextGroup?.querySelector('.review-inbox-project-heading') || $('review-inbox-pending-heading');
      (next || fallback).focus({ preventScroll: true });
    } else if (focusedHeadingProject) {
      const groups = [...pendingRoot.querySelectorAll('.review-inbox-project-card')];
      const heading = (groups.find(group => group.dataset.project === focusedHeadingProject)
        || groups.find(group => PROJECTS.indexOf(group.dataset.project) > PROJECTS.indexOf(focusedHeadingProject)))
        ?.querySelector('.review-inbox-project-heading');
      (heading || $('review-inbox-pending-heading')).focus({ preventScroll: true });
    }
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const feed = await Promise.race([window.cc.reviewInboxRead(), new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000))]);
      latest = feed && ['ok', 'missing', 'invalid', 'unavailable'].includes(feed.state) ? feed : { state: 'unavailable' };
      if (latest.state === 'ok') lastGood = latest;
    } catch { latest = { state: 'unreachable' }; }
    finally { loading = false; render(); }
  }
  async function write(item, op) {
    if (busyId || latest?.state !== 'ok') return;
    busyId = item.id; notice = ''; render();
    const opId = crypto.randomUUID();
    const firstSend = now();
    let definitive = false, response = null;
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        if (attempt > 0 && now() - firstSend >= RECEIPT_RETRY_WINDOW_MS) break;
        const controller = new AbortController();
        let deadline;
        try {
          response = await Promise.race([
            (async () => {
              const result = await fetch('/api/review-inbox/ack', { method: 'POST', credentials: 'same-origin', signal: controller.signal,
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: item.id, op, op_id: opId }) });
              return result.json();
            })(),
            new Promise((_, reject) => { deadline = setTimeout(() => { controller.abort(); reject(new Error('request_deadline')); }, requestDeadlineMs); }),
          ]);
          definitive = true; break;
        } catch { if (attempt === 2) break; }
        finally { clearTimeout(deadline); }
      }
      if (!definitive) notice = "Couldn't confirm the change. Refreshing…";
      else if (!response?.ok) notice = response?.code === 'resolved' ? 'Lews resolved this item'
        : `Couldn't ${op === 'ack' ? 'save “Got it”' : 'undo “Got it”'}. Try again.`;
      else if (response.current?.seq > response.result?.seq_seen) notice = 'Updated in another tab';
    } finally { busyId = null; await refresh(); }
  }
  window.cc.onReviewInboxChanged?.(() => refresh());
  window.addEventListener('focus', refresh);
  const timer = setInterval(refresh, 15000);
  window.addEventListener('beforeunload', () => { clearInterval(timer); resize?.disconnect(); });
  render(); refresh();
  return { refresh, close, get mode() { return mode; } };
}
module.exports = { createReviewInbox, exactPaintedMessage, safeLink, projectName, PROJECTS, STORAGE_KEY };
