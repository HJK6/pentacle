'use strict';

function trackScroll(doc, {setTimer = setTimeout, cancelTimer = clearTimeout} = {}) {
  const timers = new WeakMap();
  doc.addEventListener('scroll', event => {
    const target = event.target;
    if (!target?.classList || target.closest('.terminal,.design-tabs')) return;
    target.classList.add('next-scrolling');
    if (timers.has(target)) cancelTimer(timers.get(target));
    timers.set(target, setTimer(() => {
      target.classList.remove('next-scrolling');
      timers.delete(target);
    }, 900));
  }, { capture: true, passive: true });
}

module.exports = { trackScroll };
