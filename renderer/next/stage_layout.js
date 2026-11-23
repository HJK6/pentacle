'use strict';

const layout = require('./layout');
const INTERACTIVE = 'button,input,textarea,select,a,[role="button"],[contenteditable]';
const HEAD = '[data-grab],.next-lanes-head';

function mountStageLayout({ stage, panes, getState, onLayout, onLift }) {
  const doc = stage.ownerDocument;
  const zone = doc.createElement('div');
  zone.className = 'fluid-zone';
  zone.hidden = true;
  zone.setAttribute('aria-hidden', 'true');
  stage.append(zone);
  const grips = new Map();
  let gesture = null;
  const viewport = () => doc.defaultView.innerWidth;
  const phone = () => viewport() < layout.PHONE_WIDTH;
  const seats = () => layout.visibleSeats(getState(), viewport());
  const rect = el => el.getBoundingClientRect();
  const currentBoxes = order => Object.fromEntries(order.map(key => [key, rect(panes()[key])]));
  const sizesOf = boxes => Object.fromEntries(Object.entries(boxes).map(([key, box]) => [key, box.width]));
  const clearGlide = new WeakMap();

  function paint() {
    const order = phone() ? [] : seats();
    const current = panes();
    const width = layout.roomAt(viewport(), getState().rail);
    const gap = layout.seatGap(order, width);
    stage.style.setProperty('--fluid-gap', `${gap}px`);
    const sizes = layout.seatWidths(getState(), order, width);
    for (const [key, el] of Object.entries(current)) {
      const index = order.indexOf(key);
      el.style.order = index < 0 ? '' : String(index);
      el.style.flex = index < 0 ? '' : `0 0 ${sizes[key]}px`;
    }
    const shown = new Set();
    let left = 0;
    order.forEach((key, index) => {
      left += sizes[key];
      if (layout.canResize(key, order[index + 1])) {
        let grip = grips.get(index);
        if (!grip) {
          grip = doc.createElement('div');
          grip.className = 'grip';
          grip.setAttribute('role', 'separator');
          grip.setAttribute('aria-orientation', 'vertical');
          grip.dataset.index = String(index);
          stage.append(grip);
          grips.set(index, grip);
        }
        shown.add(index);
        grip.hidden = false;
        grip.style.left = `${left + (gap - layout.GAP) / 2}px`;
      }
      left += gap;
    });
    for (const [index, grip] of grips) if (!shown.has(index)) grip.hidden = true;
  }

  function capture(e, data) {
    const target = data.target || stage;
    gesture = { pointerId: e.pointerId, x: e.clientX, y: e.clientY, ...data, target };
    target.setPointerCapture(e.pointerId);
  }

  function begin(e) {
    if (gesture || phone() || getState().solo || e.button !== 0 || e.pointerType === 'touch') return;
    const grip = e.target.closest('.grip');
    if (grip) {
      e.preventDefault();
      const order = seats();
      const index = Number(grip.dataset.index);
      if (!layout.canResize(order[index], order[index + 1])) return;
      capture(e, { target: grip, index, order, sizes: sizesOf(currentBoxes(order)), original: getState() });
      doc.body.classList.add('dragging');
      return;
    }
    const head = e.target.closest(HEAD);
    if (!head || e.target.closest(INTERACTIVE)) return;
    const key = head.closest('[data-pane]')?.dataset.pane;
    const order = seats();
    if (order.length < 2 || !order.includes(key) || !stage.contains(head)) return;
    capture(e, { key });
  }

  function lift(g) {
    onLift();
    const order = seats(), boxes = currentBoxes(order), sizes = sizesOf(boxes);
    const b = rect(stage);
    for (const pane of Object.values(panes())) clearGlide.get(pane)?.();
    g.drag = {
      key: g.key, start: { x: g.x, y: g.y }, order, sizes, boxes,
      gap: layout.seatGap(order, layout.roomAt(viewport(), getState().rail)),
      bounds: { left: b.left, edge: b.right, top: b.top, bottom: b.bottom },
    };
    doc.body.classList.add('fluid-moving');
    panes()[g.key].classList.add('fluid-lifted');
  }

  function pose(g) {
    const d = g.drag;
    const { key, order, sizes, boxes, bounds, type, dx, dy } = d;
    const at = layout.positions(order, sizes, bounds.left, d.gap);
    const elements = panes();
    for (const k of order) {
      elements[k].style.transform = k === key ? `translate(${dx}px,${dy}px)` : `translateX(${at[k] - boxes[k].left}px)`;
    }
    const width = bounds.edge - bounds.left - layout.GAP;
    const half = width / 2;
    const target = type === 'full' ? [bounds.left, width] :
      type === 'slot' || key === 'lanes' ? [at[key], sizes[key]] :
      type === 'left' ? [bounds.left, half - (key === 'assistant' ? 0 : d.gap)] :
        [bounds.edge - layout.GAP - half + (key === 'assistant' ? 0 : d.gap), half - (key === 'assistant' ? 0 : d.gap)];
    const top = bounds.top + (getState().rail === 'top' ? 4 : layout.GAP);
    zone.hidden = false;
    zone.dataset.type = type;
    Object.assign(zone.style, { left: `${target[0]}px`, top: `${top}px`,
      width: `${target[1]}px`, height: `${bounds.bottom - top - layout.GAP}px` });
  }

  function move(e) {
    const g = gesture;
    if (!g || e.pointerId !== g.pointerId) return;
    const dx = e.clientX - g.x;
    if (g.index !== undefined) {
      if (dx || g.resized) {
        g.resized = true;
        onLayout(layout.resizeBoundary(getState(), g.order, g.sizes, g.index, dx));
      }
      return;
    }
    if (!g.drag && !layout.liftReached(dx, e.clientY - g.y)) return;
    if (!g.drag) lift(g);
    g.drag = layout.aimDrag(g.drag, e.clientX, e.clientY);
    pose(g);
  }

  function glide(before, key) {
    if (doc.defaultView.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    for (const k of seats()) {
      const el = panes()[k], from = before[k], to = rect(el);
      if (!from || (Math.abs(from.left - to.left) < 1 && Math.abs(from.top - to.top) < 1)) continue;
      el.style.transition = 'none';
      el.style.transform = `translate(${from.left - to.left}px,${from.top - to.top}px)`;
      el.classList.toggle('fluid-landing', key === k);
      el.getBoundingClientRect();
      el.style.transition = 'transform 240ms cubic-bezier(.2,.8,.2,1)';
      el.style.transform = '';
      const done = () => {
        clearTimeout(timer);
        el.style.transition = '';
        el.classList.remove('fluid-landing');
        clearGlide.delete(el);
      };
      const timer = setTimeout(done, 250);
      clearGlide.set(el, done);
    }
  }

  function end(commit, suppressClick = false) {
    const g = gesture;
    if (!g) return;
    gesture = null;
    if (g.target.hasPointerCapture(g.pointerId)) g.target.releasePointerCapture(g.pointerId);
    if (g.index !== undefined) {
      doc.body.classList.remove('dragging');
      if (!commit && g.resized) onLayout(g.original);
      return;
    }
    if (!g.drag) return;
    const before = currentBoxes(g.drag.order);
    for (const el of Object.values(panes())) {
      el.style.transform = '';
      el.classList.remove('fluid-lifted');
    }
    doc.body.classList.remove('fluid-moving');
    zone.hidden = true;
    if (suppressClick) {
      const eat = e => { e.stopImmediatePropagation(); e.preventDefault(); };
      doc.addEventListener('click', eat, { capture: true, once: true });
      setTimeout(() => doc.removeEventListener('click', eat, true), 0);
    }
    if (commit) onLayout(layout.dropSection(getState(), g.drag, viewport()));
    glide(before, g.key);
  }

  function matches(e) { return e.pointerId === gesture?.pointerId; }
  stage.addEventListener('pointerdown', begin);
  stage.addEventListener('pointermove', move);
  stage.addEventListener('pointerup', e => { if (matches(e)) end(true, true); });
  stage.addEventListener('pointercancel', e => { if (matches(e)) end(false); });
  stage.addEventListener('lostpointercapture', e => { if (matches(e)) end(false); });
  stage.addEventListener('dblclick', e => { if (e.target.closest('.grip')) onLayout(layout.resetWidths(getState())); });
  doc.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !gesture || (!gesture.drag && gesture.index === undefined)) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    end(false);
  }, true);
  return { paint, cancel() { end(false); } };
}

module.exports = { mountStageLayout };
