'use strict';

function trackScroll(doc, {setTimer = setTimeout, cancelTimer = clearTimeout} = {}) {
  const timers = new WeakMap();
  const eligible = target => target?.classList && !target.closest('.terminal,.design-tabs');
  const options = { capture: true, passive: true };
  // Capture handles pointerenter/leave even on a newly mounted scroll area.
  // A class change invalidates Chrome's native scrollbar paint; :hover alone
  // can leave the thumb with stale resting pixels.
  doc.addEventListener('pointerenter', event => {
    const target = event.target;
    if (event.pointerType === 'touch' || !eligible(target)) return;
    const style = doc.defaultView.getComputedStyle(target);
    if (['auto','scroll'].some(value => value === style.overflowX || value === style.overflowY || value === style.overflow))
      target.classList.add('next-scroll-near');
  }, options);
  doc.addEventListener('pointerleave', event => {
    if (event.pointerType !== 'touch') event.target?.classList?.remove('next-scroll-near');
  }, options);
  doc.addEventListener('scroll', event => {
    const target = event.target;
    if (!eligible(target)) return;
    target.classList.add('next-scrolling');
    if (timers.has(target)) cancelTimer(timers.get(target));
    timers.set(target, setTimer(() => {
      target.classList.remove('next-scrolling');
      timers.delete(target);
    }, 900));
  }, options);
}

module.exports = { trackScroll };
