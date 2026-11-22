'use strict';

const { DESIGN_FRAME_NAME, DESIGN_FRAME_KEYS } = require('../../lib/design_frame.cjs');
const { arrowGeometry, fitBoard, fitScreen, keepScreenLabel, stepScreen, zoomCamera } = require('./design_board');

const svgNamespace = 'http://www.w3.org/2000/svg';
let markerNumber = 0;
const pointerIcon = '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="m3.6 2.4 9.4 4.8L9 8.5l2.3 4.3-1.7.9-2.3-4.3L4.3 12z"/></svg>';

function designMediaUrl(project, view) {
  const file = view?.screenshots?.[0]?.path;
  if (!file) return '';
  return '/design-media/' + project.id + '/' + project.revision_id + '/' +
    file.split('/').map(encodeURIComponent).join('/');
}

function element(document, tag, className, parent, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  parent?.append(node);
  return node;
}

function button(document, parent, label, text, action, value) {
  const node = element(document, 'button', '', parent, text);
  node.type = 'button';
  node.setAttribute('aria-label', label);
  node.title = label;
  node.dataset.action = action;
  if (value !== undefined) node.dataset.value = value;
  return node;
}

function putRect(element, rect) {
  element.style.left = rect.x + 'px';
  element.style.top = rect.y + 'px';
  element.style.width = rect.w + 'px';
  element.style.height = rect.h + 'px';
}

function midpoint(a, b) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function mountDesignCanvas(root, options) {
  const { project, design, phone, onScreenChange, onBack } = options;
  const document = root.ownerDocument, window = document.defaultView;
  const fetch = options.fetch || window.fetch.bind(window);
  const lifecycle = new window.AbortController();
  const listen = { signal: lifecycle.signal };
  const nodes = design.nodes.map(node => ({ ...node }));
  const screens = nodes.filter(node => node.type === 'screen');
  const sections = nodes.filter(node => node.type === 'section');
  const byView = new Map(screens.map(screen => [screen.view, screen]));
  const items = new Map((project.manifest?.views || []).map(item => [item.id, item]));
  const screenElements = new Map();
  const pointers = new Map();

  let current = options.screenId && byView.has(options.screenId) ? options.screenId : null;
  if (!current && screens.length === 1) current = screens[0].view;
  let menu = false, moved = false;
  let overview = options.overview ?? (!current || screens.length === 1 && !options.screenId);
  let camera = options.camera || { x: 0, y: 0, z: 1 };
  let fitZoom = 1, frame, frameRequest, animation = 0, skipClick = false;
  let gesture, labelItems = [];
  let positioned = !!options.camera;
  let previousSize;

  root.innerHTML = [
    '<div class="design-stage" aria-label="Design canvas" tabindex="0"><div class="design-world"></div></div>',
    '<div class="design-outline" role="region" aria-label="Screen list" hidden></div>',
    '<div class="design-board-tools"></div>',
  ].join('');
  const viewport = root.querySelector('.design-stage');
  const world = root.querySelector('.design-world');
  const tools = root.querySelector('.design-board-tools');
  const outline = root.querySelector('.design-outline');
  const arrows = document.createElementNS(svgNamespace, 'svg');
  const paths = document.createElementNS(svgNamespace, 'g');
  const markerId = 'design-arrow-' + ++markerNumber;
  arrows.setAttribute('class', 'design-arrows');
  arrows.setAttribute('aria-hidden', 'true');
  const definition = document.createElementNS(svgNamespace, 'defs');
  const marker = document.createElementNS(svgNamespace, 'marker');
  marker.id = markerId;
  marker.setAttribute('viewBox', '0 0 10 10');
  marker.setAttribute('refX', '9');
  marker.setAttribute('refY', '5');
  marker.setAttribute('markerWidth', '6');
  marker.setAttribute('markerHeight', '6');
  marker.setAttribute('orient', 'auto');
  const triangle = document.createElementNS(svgNamespace, 'path');
  triangle.setAttribute('d', 'M0 0 L10 5 L0 10 Z');
  triangle.setAttribute('class', 'design-arrow-head');
  marker.append(triangle); definition.append(marker); arrows.append(definition, paths);
  world.append(arrows);
  const arrowLabels = element(document, 'div', 'design-arrow-labels', world);

  const reset = button(document, tools, 'Reset layout', 'Reset layout', 'reset');
  reset.className = 'design-reset';
  const navigation = element(document, 'div', 'design-step design-pill', tools);
  const previous = button(document, navigation, 'Previous screen', '‹', 'previous');
  const count = button(document, navigation, 'Screens', '', 'outline');
  const next = button(document, navigation, 'Next screen', '›', 'next');
  const zoom = element(document, 'div', 'design-zoom design-pill', tools);
  button(document, zoom, 'Zoom out', '−', 'out');
  const scale = element(document, 'span', 'design-scale', zoom);
  button(document, zoom, 'Zoom in', '+', 'in');
  const fit = button(document, zoom, 'Show everything', '⛶', 'fit');
  fit.className = 'design-fit';

  function viewSize() {
    return { width: viewport.clientWidth, height: viewport.clientHeight };
  }

  function viewCenter() {
    const { width, height } = viewSize();
    return { x: width / 2, y: height / 2 };
  }

  function position(point) {
    const rect = viewport.getBoundingClientRect();
    return { x: point.clientX - rect.left, y: point.clientY - rect.top };
  }

  function layoutArrows() {
    paths.replaceChildren();
    arrowLabels.replaceChildren();
    labelItems = [];
    for (const arrow of nodes.filter(node => node.type === 'arrow')) {
      const from = byView.get(arrow.from), to = byView.get(arrow.to);
      if (!from || !to) continue;
      const geometry = arrowGeometry(from, to);
      const path = document.createElementNS(svgNamespace, 'path');
      path.setAttribute('d', geometry.d);
      path.setAttribute('marker-end', 'url(#' + markerId + ')');
      paths.append(path);
      if (!arrow.label) continue;
      const anchor = element(document, 'span', 'design-arrow-anchor', arrowLabels);
      anchor.style.left = geometry.label.x + 'px';
      anchor.style.top = geometry.label.y + 'px';
      const label = element(document, 'span', 'design-arrow-label', anchor, arrow.label);
      labelItems.push({ anchor, label, length: geometry.length });
    }
  }

  function makeNode(node, index) {
    if (node.type === 'arrow') return;
    if (node.type === 'section' || node.type === 'note') {
      const area = element(document, 'div', 'design-node design-' + node.type, world);
      putRect(area, node);
      const text = node.type === 'section' ? node.title : node.text;
      button(document, area, text, text, 'fit-node', String(index)).className = 'design-node-title';
      return;
    }
    const item = items.get(node.view);
    const area = element(document, 'div', 'design-node design-screen', world);
    area.dataset.view = node.view;
    if (node.w < node.h) area.dataset.phone = '';
    putRect(area, node);
    const title = element(document, 'div', 'design-screen-label', area);
    const name = button(document, title, node.title, node.title, 'screen', node.view);
    name.dataset.moveView = node.view;
    const payload = element(document, 'div', 'design-screen-payload', area);
    const image = designMediaUrl(project, item);
    if (image) {
      const still = element(document, 'img', 'design-still', payload);
      still.src = image; still.alt = ''; still.draggable = false; still.decoding = 'async';
    }
    const target = button(document, payload, node.title, '', 'screen', node.view);
    target.className = 'design-screen-hit';
    const prototype = item?.variant === 'prototype';
    if (prototype) {
      area.classList.add('is-prototype');
      const mark = element(document, 'span', 'design-prototype-mark');
      mark.innerHTML = pointerIcon;
      title.prepend(mark);
      element(document, 'span', 'design-prototype-badge', payload).innerHTML = pointerIcon;
    }
    const restart = prototype ? button(document, title, 'Start over', 'Start over', 'restart') : null;
    if (restart) restart.className = 'design-restart';
    screenElements.set(node.view, { area, title, payload, item, restart });
  }

  [...sections, ...nodes.filter(node => node.type !== 'section')].forEach(node => makeNode(node, nodes.indexOf(node)));
  layoutArrows();

  function dropFrame() {
    frameRequest?.abort();
    frameRequest = null;
    frame?.remove();
    frame = null;
  }

  async function loadFrame() {
    dropFrame();
    const entry = screenElements.get(current);
    if (!entry?.item?.prototype) return;
    const request = new window.AbortController();
    frameRequest = request;
    try {
      const url = ['/api/designs', project.id, project.revision_id, current].join('/');
      const reply = await fetch(url, { signal: request.signal });
      if (!reply.ok) return;
      const data = await reply.json();
      if (request.signal.aborted || typeof data.document !== 'string') return;
      frame = element(document, 'iframe', 'design-live');
      frame.name = DESIGN_FRAME_NAME;
      frame.title = entry.item.title;
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.srcdoc = data.document;
      entry.payload.append(frame);
      paintCamera();
    } catch (error) {
      // A missing prepared document leaves the recorded screen visible.
      if (!request.signal.aborted) entry.area.dataset.preview = 'still';
    }
  }

  function paintControls() {
    reset.hidden = !moved;
    navigation.hidden = screens.length < 2;
    outline.hidden = !menu;
    count.setAttribute('aria-expanded', String(menu));
    const index = screens.findIndex(screen => screen.view === current);
    count.textContent = index === -1 ? screens.length + ' screens' : (index + 1) + ' / ' + screens.length;
    previous.disabled = index === 0;
    next.disabled = index === screens.length - 1;
    for (const [id, rendered] of screenElements) {
      rendered.area.classList.toggle('is-current', id === current);
      if (rendered.restart) rendered.restart.hidden = id !== current;
    }
    for (const option of outline.querySelectorAll('[data-action=screen]')) {
      option.setAttribute('aria-pressed', String(option.dataset.value === current));
    }
  }

  function fillOutline() {
    outline.replaceChildren();
    const groups = new Map([[null, []], ...sections.map(section => [section, []])]);
    for (const screen of screens) {
      const x = screen.x + screen.w / 2, y = screen.y + screen.h / 2;
      const section = sections.find(section => x > section.x && x < section.x + section.w &&
        y > section.y && y < section.y + section.h);
      groups.get(section ?? null).push(screen);
    }
    for (const [section, entries] of groups) {
      if (!entries.length) continue;
      if (section) element(document, 'h3', 'design-outline-section', outline, section.title);
      for (const screen of entries) {
        button(document, outline, screen.title, screen.title, 'screen', screen.view);
      }
    }
  }

  function paintCamera() {
    world.style.transform = 'translate(' + camera.x + 'px,' + camera.y + 'px) scale(' + camera.z + ')';
    world.style.setProperty('--design-inverse-scale', String(1 / camera.z));
    world.style.setProperty('--design-scale-factor', String(camera.z));
    scale.textContent = Math.round(camera.z * 100) + '%';
    for (const [id, rendered] of screenElements) {
      const screen = byView.get(id);
      rendered.title.hidden = !keepScreenLabel(screen.w, camera.z, phone());
      rendered.area.toggleAttribute('data-label-hidden', rendered.title.hidden);
    }
    for (const { anchor, label, length } of labelItems) {
      anchor.classList.toggle('too-short', length * camera.z < label.offsetWidth + 12);
    }
    if (frame && current) {
      const ideal = fitScreen(byView.get(current), viewSize(), phone()).z;
      const acceptsInput = camera.z >= ideal * 0.7;
      frame.dataset.liveInput = String(acceptsInput);
      frame.tabIndex = acceptsInput ? 0 : -1;
    }
  }

  function stopAnimation() {
    if (animation) window.cancelAnimationFrame(animation);
    animation = 0;
  }

  function moveCamera(target, animate = true) {
    stopAnimation();
    if (!animate || !window.requestAnimationFrame ||
        window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      camera = target; paintCamera(); return;
    }
    const start = camera, midpoint = viewCenter();
    const from = { x: (midpoint.x - start.x) / start.z, y: (midpoint.y - start.y) / start.z };
    const to = { x: (midpoint.x - target.x) / target.z, y: (midpoint.y - target.y) / target.z };
    const began = window.performance.now();
    function tick(time) {
      const progress = Math.min(1, (time - began) / 300);
      const eased = 1 - (1 - progress) ** 3;
      const z = start.z * (target.z / start.z) ** eased;
      camera = {
        x: midpoint.x - (from.x + (to.x - from.x) * eased) * z,
        y: midpoint.y - (from.y + (to.y - from.y) * eased) * z,
        z,
      };
      paintCamera();
      animation = progress < 1 ? window.requestAnimationFrame(tick) : 0;
    }
    animation = window.requestAnimationFrame(tick);
  }

  function resize() {
    const size = viewSize();
    if (!size.width || !size.height) return;
    const phoneUi = phone();
    if (previousSize?.width === size.width && previousSize.height === size.height && previousSize.phone === phoneUi) return;
    const preserveCamera = !previousSize && positioned;
    previousSize = { ...size, phone: phoneUi };
    const whole = fitBoard(nodes, size, phoneUi);
    fitZoom = whole.z;
    if (preserveCamera) paintCamera();
    else if (!positioned || overview) {
      moveCamera(current && !overview ? fitScreen(byView.get(current), size, phone()) : whole, false);
      positioned = true;
    } else paintCamera();
  }

  function setCurrent(id) {
    const changed = current !== id;
    current = id;
    menu = false;
    if (changed) void loadFrame();
    paintControls();
    onScreenChange?.(current);
  }

  function selectScreen(id) {
    const screen = byView.get(id);
    if (!screen) return;
    setCurrent(id);
    overview = false;
    moveCamera(fitScreen(screen, viewSize(), phone()));
    viewport.focus({ preventScroll: true });
  }

  function overviewBoard() {
    overview = true;
    if (screens.length !== 1) setCurrent(null);
    moveCamera(fitBoard(nodes, viewSize(), phone()));
  }

  function zoomAt(factor, anchor = viewCenter()) {
    overview = false;
    moveCamera(zoomCamera(camera, factor, anchor, fitZoom), false);
  }

  function step(delta) {
    const screen = stepScreen(screens, current, delta);
    if (screen) selectScreen(screen.view);
  }

  function handleKey(key) {
    switch (key) {
      case 'ArrowDown':
      case 'ArrowRight':
        if (screens.length < 2) return false;
        step(1); return true;
      case 'ArrowUp':
      case 'ArrowLeft':
        if (screens.length < 2) return false;
        step(-1); return true;
      case '0': overviewBoard(); return true;
      case '+':
      case '=': zoomAt(1.25); return true;
      case '-': zoomAt(0.8); return true;
      case 'Escape':
        if (menu) { menu = false; paintControls(); }
        else if (!overview) overviewBoard();
        else onBack();
        return true;
      default: return false;
    }
  }

  root.addEventListener('click', event => {
    if (skipClick && viewport.contains(event.target)) {
      skipClick = false;
      event.preventDefault();
      return;
    }
    const control = event.target.closest?.('[data-action]');
    if (!control || !root.contains(control)) return;
    switch (control.dataset.action) {
      case 'screen': selectScreen(control.dataset.value); break;
      case 'previous': step(-1); break;
      case 'next': step(1); break;
      case 'outline':
        menu = !menu;
        if (menu) fillOutline();
        paintControls();
        break;
      case 'restart': void loadFrame(); break;
      case 'fit': overviewBoard(); break;
      case 'out': zoomAt(0.8); break;
      case 'in': zoomAt(1.25); break;
      case 'fit-node':
        overview = false;
        moveCamera(fitScreen(nodes[Number(control.dataset.value)], viewSize(), phone()));
        break;
      case 'reset':
        nodes.forEach((node, index) => {
          if (node.type !== 'screen') return;
          Object.assign(node, { x: design.nodes[index].x, y: design.nodes[index].y });
          putRect(screenElements.get(node.view).area, node);
        });
        moved = false; layoutArrows(); paintControls(); overviewBoard();
        break;
    }
  }, listen);
  viewport.addEventListener('wheel', event => {
    event.preventDefault();
    zoomAt(Math.exp(-event.deltaY * (event.ctrlKey ? 0.012 : 0.002)), position(event));
  }, { ...listen, passive: false });
  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    if (!pointers.size) {
      skipClick = false;
      viewport.focus({ preventScroll: true });
    }
    const point = position(event);
    pointers.set(event.pointerId, point);
    if (pointers.size === 1) {
      const handle = event.pointerType === 'mouse' && event.target.closest?.('[data-move-view]');
      gesture = { start: point, camera: { ...camera }, screen: handle && byView.get(handle.dataset.moveView) };
      if (gesture.screen) gesture.origin = { x: gesture.screen.x, y: gesture.screen.y };
    } else if (pointers.size === 2) {
      const [a, b] = pointers.values();
      gesture = { camera: { ...camera }, pinchStart: midpoint(a, b), pinchDistance: Math.max(1, distance(a, b)) };
      skipClick = true;
    }
  }, listen);
  viewport.addEventListener('pointermove', event => {
    if (!gesture || !pointers.has(event.pointerId)) return;
    const point = position(event);
    pointers.set(event.pointerId, point);
    if (pointers.size === 2 && gesture.pinchStart) {
      stopAnimation();
      const [a, b] = pointers.values(), center = midpoint(a, b);
      const target = zoomCamera(gesture.camera, distance(a, b) / gesture.pinchDistance, gesture.pinchStart, fitZoom);
      camera = { ...target, x: target.x + center.x - gesture.pinchStart.x, y: target.y + center.y - gesture.pinchStart.y };
      overview = false; paintCamera(); return;
    }
    if (pointers.size !== 1 || !gesture.start) return;
    const dx = point.x - gesture.start.x, dy = point.y - gesture.start.y;
    if (!skipClick && Math.hypot(dx, dy) < 5) return;
    skipClick = true;
    if (!viewport.hasPointerCapture(event.pointerId)) viewport.setPointerCapture(event.pointerId);
    stopAnimation();
    overview = false;
    if (gesture.screen) {
      const screen = gesture.screen;
      screen.x = gesture.origin.x + dx / camera.z;
      screen.y = gesture.origin.y + dy / camera.z;
      moved = true;
      putRect(screenElements.get(screen.view).area, screen);
      layoutArrows(); paintControls();
    } else {
      camera = { ...gesture.camera, x: gesture.camera.x + dx, y: gesture.camera.y + dy };
    }
    paintCamera();
  }, listen);
  function pointerEnd(event) {
    pointers.delete(event.pointerId);
    if (!pointers.size) gesture = null;
    else if (pointers.size === 1) gesture = { start: [...pointers.values()][0], camera: { ...camera } };
  }
  viewport.addEventListener('pointerup', pointerEnd, listen);
  viewport.addEventListener('pointercancel', pointerEnd, listen);
  viewport.addEventListener('pointerleave', event => {
    if (!viewport.hasPointerCapture(event.pointerId)) pointerEnd(event);
  }, listen);
  window.addEventListener('message', event => {
    if (frame && event.source === frame.contentWindow && event.origin === 'null' && DESIGN_FRAME_KEYS.includes(event.data)) {
      handleKey(event.data);
    }
  }, listen);

  fillOutline(); paintControls(); resize();
  if (current) void loadFrame();
  return {
    handleKey, resize, selectScreen,
    snapshot: () => ({ camera: { ...camera }, current, overview }),
    focus: () => viewport.focus({ preventScroll: true }),
    destroy() { lifecycle.abort(); stopAnimation(); dropFrame(); root.replaceChildren(); },
  };
}

module.exports = { designMediaUrl, mountDesignCanvas, pointerIcon };
