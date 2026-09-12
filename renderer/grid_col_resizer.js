'use strict';

const COLUMN_MIN = 220;
function normalizeSplit(value) {
  const n = typeof value === 'number' || (typeof value === 'string' && value.trim()) ? Number(value) : NaN;
  return Number.isFinite(n) && n > 0 && n < 1 ? n : .5;
}
function clampSplit(width, gap, preferred) {
  const available = Math.max(0, width - Math.max(0, gap || 0));
  if (!Number.isFinite(available) || available <= 0) return null;
  const min = Math.min(.5, COLUMN_MIN / available), max = 1 - min;
  const fraction = Math.max(min, Math.min(max, Number.isFinite(preferred) ? preferred : .5));
  return { available, min, max, fraction, left: available * fraction, right: available * (1 - fraction), movable: min < max };
}

// One grid, one preference. The effective clamp is deliberately not saved:
// shrinking a window must not erase the user's wide-window split.
function createGridColResizer({ grid, handle, initialSplit, save, onResize = () => {}, isVisible = () => true, emit = () => {} }) {
  const win = grid.ownerDocument.defaultView;
  let preferred = normalizeSplit(initialSplit), geometry = null, gesture = null, lastTap = null;
  let frame = null, destroyed = false;
  const listeners = [];
  const on = (target, type, fn) => { target.addEventListener(type, fn); listeners.push(() => target.removeEventListener(type, fn)); };
  function visible() { return isVisible() && !grid.classList.contains('maximized'); }
  function measure() {
    const css = win.getComputedStyle(grid);
    const gap = parseFloat(css.columnGap) || 0;
    const padding = (parseFloat(css.paddingLeft) || 0) + (parseFloat(css.paddingRight) || 0);
    const width = grid.clientWidth - padding;
    return { gap, paddingLeft: parseFloat(css.paddingLeft) || 0, borderLeft: parseFloat(css.borderLeftWidth) || 0, ...clampSplit(width, gap, preferred) };
  }
  function evidence(name) {
    emit(name, { subsystem: 'slot-layout', bug_ref: 'spec_pentacle__resizable_panel_split', preferred, effective: geometry?.fraction ?? preferred, left: geometry?.left ?? 0, right: geometry?.right ?? 0 });
  }
  function apply() {
    if (destroyed || !visible()) return;
    const next = measure();
    if (!next.available) return;
    const changed = !geometry || next.left !== geometry.left || next.right !== geometry.right;
    geometry = next;
    grid.style.setProperty('--col-left', `${next.fraction}fr`);
    grid.style.setProperty('--col-right', `${1 - next.fraction}fr`);
    handle.style.left = `${next.paddingLeft + next.left + next.gap / 2}px`;
    handle.setAttribute('aria-valuemin', String(Math.round(next.min * 100)));
    handle.setAttribute('aria-valuemax', String(Math.round(next.max * 100)));
    handle.setAttribute('aria-valuenow', String(Math.round(next.fraction * 100)));
    handle.setAttribute('aria-disabled', String(!next.movable));
    if (changed) onResize();
  }
  function schedule() {
    if (frame !== null || destroyed) return;
    frame = win.requestAnimationFrame(() => { frame = null; apply(); });
  }
  function flush() {
    if (frame !== null) win.cancelAnimationFrame(frame);
    frame = null;
    apply();
  }
  function release() {
    const old = gesture;
    gesture = null;
    grid.classList.remove('resizing-columns');
    if (old && handle.hasPointerCapture?.(old.id)) handle.releasePointerCapture(old.id);
  }
  function cancel() {
    if (!gesture) return;
    preferred = gesture.before;
    release();
    lastTap = null;
    flush();
    onResize();
    evidence('cancel');
  }
  function refresh() {
    if (!visible()) { cancel(); return; }
    if (gesture && !measure().movable) cancel();
    const before = geometry;
    apply();
    if (!gesture && geometry !== before && (geometry?.left !== before?.left || geometry?.right !== before?.right)) evidence('layout');
  }
  function setFromPointer(event) {
    const box = grid.getBoundingClientRect(), next = measure();
    if (!next.available) return;
    preferred = clampSplit(next.available, 0,
      (event.clientX - box.left - next.borderLeft - next.paddingLeft - next.gap / 2) / next.available).fraction;
  }
  function commit(name) {
    flush();
    save(preferred);
    onResize();
    evidence(name);
  }
  on(handle, 'pointerdown', event => {
    if (gesture || event.isPrimary === false || event.button !== 0 || !visible()) return;
    refresh();
    if (!geometry?.movable) return;
    event.preventDefault();
    handle.focus({ preventScroll: true });
    gesture = { id:event.pointerId, type:event.pointerType, x:event.clientX, y:event.clientY, at:Date.now(), before:preferred, moved:false };
    handle.setPointerCapture(event.pointerId);
    grid.classList.add('resizing-columns');
  });
  on(handle, 'pointermove', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    if (!visible()) { cancel(); return; }
    if (Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > 8) gesture.moved = true;
    if (!gesture.moved) return;
    event.preventDefault();
    setFromPointer(event);
    schedule();
  });
  on(handle, 'pointerup', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    if (!visible()) { cancel(); return; }
    const old = gesture, now = Date.now();
    const moved = old.moved || Math.hypot(event.clientX - old.x, event.clientY - old.y) > 8;
    release();
    if (moved) {
      lastTap = null;
      setFromPointer(event);
      commit('commit');
    } else if (now - old.at <= 300) {
      if (lastTap && lastTap.type === old.type && now - lastTap.at <= 350 && Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) <= 24) {
        lastTap = null;
        preferred = .5;
        commit('reset');
      } else lastTap = { type:old.type, at:now, x:event.clientX, y:event.clientY };
    } else lastTap = null;
  });
  // Pointer gestures cover mouse and touch exactly once; the synthesized
  // click/dblclick events carry no reliable movement information.
  on(handle, 'dblclick', event => event.preventDefault());
  for (const type of ['pointercancel', 'lostpointercapture']) on(handle, type, event => {
    if (gesture?.id === event.pointerId) cancel();
  });
  on(win, 'blur', () => { cancel(); lastTap = null; });
  on(handle, 'keydown', event => {
    if (!visible() || gesture) return;
    refresh();
    if (!geometry?.movable) return;
    let next;
    if (event.key === 'ArrowLeft') next = geometry.fraction - .02;
    else if (event.key === 'ArrowRight') next = geometry.fraction + .02;
    else if (event.key === 'Home') next = geometry.min;
    else if (event.key === 'End') next = geometry.max;
    else if (event.key === 'Enter') next = .5;
    else return;
    event.preventDefault();
    lastTap = null;
    preferred = Math.max(geometry.min, Math.min(geometry.max, next));
    commit(event.key === 'Enter' ? 'reset' : 'commit');
  });
  const observer = win.ResizeObserver ? new win.ResizeObserver(refresh) : null;
  observer?.observe(grid);
  on(win, 'resize', refresh);
  refresh();
  return { refresh, cancel, destroy() { cancel(); destroyed = true; if (frame !== null) win.cancelAnimationFrame(frame); observer?.disconnect(); listeners.forEach(remove => remove()); } };
}
module.exports = { COLUMN_MIN, normalizeSplit, clampSplit, createGridColResizer };
