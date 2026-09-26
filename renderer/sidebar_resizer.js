'use strict';

const SIDEBAR_MIN = 260;
const SLOT_MIN = 220;
const KEY_STEP = 10;

function normalizeSidebarWidth(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= SIDEBAR_MIN ? number : SIDEBAR_MIN;
}

function sidebarBounds(containerWidth, differences = [], twoSlot = true, gap = 1) {
  const width = Math.max(0, Number(containerWidth) || 0);
  const largestDifference = twoSlot ? Math.max(0, ...differences.map(value => Math.abs(Number(value) || 0))) : 0;
  const contentFloor = twoSlot ? 2 * SLOT_MIN + largestDifference + gap : SLOT_MIN;
  return { min: SIDEBAR_MIN, max: Math.max(SIDEBAR_MIN, width - contentFloor) };
}

function clampSidebarWidth(value, limits) {
  return Math.max(limits.min, Math.min(limits.max, Number(value) || limits.min));
}

function splitForDifference(available, difference) {
  const width = Math.max(0, Number(available) || 0);
  if (!width) return { left: 0, right: 0, fraction: .5 };
  const target = Math.max(-width + 2 * SLOT_MIN, Math.min(width - 2 * SLOT_MIN, Number(difference) || 0));
  const left = (width + target) / 2;
  return { left, right: width - left, fraction: left / width };
}

function createSidebarResizer({ main, sidebar, handle, grid, rows, rowResizers,
  initialWidth, save, onResize = () => {}, onNarrow = () => {} }) {
  const win = main.ownerDocument.defaultView;
  let preferred = normalizeSidebarWidth(initialWidth);
  let gesture = null;
  let destroyed = false;
  let frame = null;
  const listeners = [];
  const on = (target, name, callback) => { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); };
  const narrow = () => main.clientWidth < SIDEBAR_MIN + 2 * SLOT_MIN + 1;
  const twoSlot = () => !narrow() && !grid.classList.contains('maximized');
  const differences = () => twoSlot() ? rows.map(row => {
    const cells = row.querySelectorAll('.grid-cell');
    return cells.length > 1 ? cells[0].getBoundingClientRect().width - cells[1].getBoundingClientRect().width : 0;
  }) : [];
  const limits = (diffs = differences()) => sidebarBounds(main.clientWidth, diffs, twoSlot());
  const width = () => sidebar.getBoundingClientRect().width;
  function aria(lim = limits()) {
    handle.setAttribute('aria-valuemin', String(Math.round(lim.min)));
    handle.setAttribute('aria-valuemax', String(Math.round(lim.max)));
    handle.setAttribute('aria-valuenow', String(Math.round(width())));
    handle.setAttribute('aria-disabled', String(lim.max <= lim.min));
  }
  function apply(next, diffs = null) {
    const lim = limits(diffs || differences());
    const effective = clampSidebarWidth(next, lim);
    main.style.setProperty('--sidebar-width', `${effective}px`);
    // Force one layout before updating the row fractions so their new
    // available width is measured against this exact sidebar width.
    width();
    onNarrow(narrow());
    if (diffs && twoSlot()) rowResizers.forEach((resizer, index) => resizer?.setPixelDifference(diffs[index] || 0));
    else rowResizers.forEach(resizer => resizer?.refresh());
    aria(lim);
    onResize();
    return effective;
  }
  function commit(next, diffs) {
    preferred = apply(next, diffs);
    save(preferred);
    if (diffs && twoSlot()) rowResizers.forEach(resizer => resizer?.commitPreference());
  }
  function release() {
    const old = gesture; gesture = null;
    main.classList.remove('resizing-sidebar');
    if (old && handle.hasPointerCapture?.(old.id)) handle.releasePointerCapture(old.id);
  }
  function cancel() {
    if (!gesture) return;
    const old = gesture;
    release();
    apply(old.width);
    rowResizers.forEach((resizer, index) => resizer?.setPreference(old.splits[index]));
    aria();
  }
  function refresh() {
    if (destroyed) return;
    if (gesture) cancel();
    apply(preferred);
  }
  on(handle, 'pointerdown', event => {
    if (gesture || event.isPrimary === false || event.button !== 0) return;
    const lim = limits();
    if (lim.max <= lim.min) return;
    event.preventDefault();
    handle.focus({ preventScroll: true });
    gesture = { id: event.pointerId, x: event.clientX, width: width(),
      differences: differences(), splits: rowResizers.map(resizer => resizer?.getPreferred()) };
    handle.setPointerCapture(event.pointerId);
    main.classList.add('resizing-sidebar');
  });
  on(handle, 'pointermove', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    event.preventDefault();
    if (frame !== null) win.cancelAnimationFrame(frame);
    const requested = gesture.width + event.clientX - gesture.x;
    const diffs = gesture.differences;
    frame = win.requestAnimationFrame(() => { frame = null; if (gesture) apply(requested, diffs); });
  });
  on(handle, 'pointerup', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    event.preventDefault();
    if (frame !== null) { win.cancelAnimationFrame(frame); frame = null; }
    const old = gesture;
    const next = old.width + event.clientX - old.x;
    release();
    commit(next, old.differences);
  });
  for (const name of ['pointercancel', 'lostpointercapture']) on(handle, name, event => {
    if (gesture?.id === event.pointerId) cancel();
  });
  on(win, 'blur', cancel);
  on(handle, 'keydown', event => {
    if (event.key === 'Escape' && gesture) { event.preventDefault(); cancel(); return; }
    if (gesture) return;
    const lim = limits();
    let next;
    if (event.key === 'ArrowRight') next = width() + KEY_STEP;
    else if (event.key === 'ArrowLeft') next = width() - KEY_STEP;
    else if (event.key === 'Home') next = lim.min;
    else if (event.key === 'End') next = lim.max;
    else return;
    event.preventDefault();
    commit(next, differences());
  });
  on(win, 'resize', refresh);
  const observer = win.ResizeObserver ? new win.ResizeObserver(refresh) : null;
  observer?.observe(main);
  refresh();
  return { refresh, cancel, getPreferred: () => preferred,
    destroy() { cancel(); destroyed = true; if (frame !== null) win.cancelAnimationFrame(frame); observer?.disconnect(); listeners.forEach(remove => remove()); } };
}

module.exports = { SIDEBAR_MIN, SLOT_MIN, KEY_STEP, normalizeSidebarWidth,
  sidebarBounds, clampSidebarWidth, splitForDifference, createSidebarResizer };
