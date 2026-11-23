'use strict';

const KEY = 'pentacle.next.read';
const DEFAULT_WIDTH = 720;
const SLIDER_MIN = 480;
const SLIDER_MAX = 1200;

function parseReadingWidth(raw) {
  const value = String(raw ?? '');
  if (!/^\d{3,4}$/.test(value)) return null;
  const width = Number(value);
  return width >= 320 && width <= 2400 ? width : null;
}

function sliderPosition(width) {
  return Math.min(SLIDER_MAX, Math.max(SLIDER_MIN, width));
}

function createReadingWidth(root, storage) {
  let width = DEFAULT_WIDTH;
  try { width = parseReadingWidth(storage?.getItem(KEY)) ?? DEFAULT_WIDTH; } catch {}
  const paint = () => root.style.setProperty('--read', width + 'px');
  paint();
  return {
    get width() { return width; },
    set(raw) {
      const value = parseReadingWidth(raw);
      if (value === null) return false;
      if (value !== width) {
        width = value;
        paint();
        try { storage?.setItem(KEY, String(width)); } catch {}
      }
      return true;
    },
    guide(value) { root.classList.toggle('next-read-guides', value); },
  };
}

module.exports = { parseReadingWidth, sliderPosition, createReadingWidth };
