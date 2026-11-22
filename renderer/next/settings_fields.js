'use strict';

const { sliderPosition } = require('./reading_width');

function mountSettingsFields(host, { reading, microphone }) {
  const doc = host.ownerDocument;
  const block = doc.createElement('section');
  block.className = 'next-width-setting';
  block.innerHTML = '<div class="next-setting-row"><label for="next-text-width">Text width</label>' +
    '<span class="next-width-value"><input id="next-text-width" type="text" inputmode="numeric" maxlength="4" autocomplete="off">' +
    '<span aria-hidden="true">px</span></span></div>' +
    '<input class="next-width-slider" type="range" min="480" max="1200" step="10" aria-label="Text width slider">';
  host.append(block);
  const field = block.querySelector('#next-text-width');
  const slider = block.querySelector('.next-width-slider');
  const sync = ({keepField = false} = {}) => {
    if (!keepField) field.value = String(reading.width);
    slider.value = String(sliderPosition(reading.width));
    slider.setAttribute('aria-valuetext', reading.width + ' pixels');
    slider.style.setProperty('--read-progress', ((Number(slider.value) - 480) / 720) * 100 + '%');
  };
  field.addEventListener('input', () => { if (reading.set(field.value)) sync({keepField: true}); });
  field.addEventListener('blur', () => sync());
  field.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); field.blur(); }
  });
  slider.addEventListener('input', () => { reading.set(slider.value); sync(); });
  block.addEventListener('focusin', e => { if (e.target === field || e.target === slider) reading.guide(true); });
  block.addEventListener('focusout', e => {
    if (e.relatedTarget !== field && e.relatedTarget !== slider) reading.guide(false);
  });
  sync();

  const mic = doc.createElement('section');
  mic.className = 'next-mic-setting';
  mic.innerHTML = '<div id="next-mic-heading">Microphone</div><button type="button" class="next-mic-choice" ' +
    'aria-expanded="false" aria-controls="next-mic-options"><span></span><i aria-hidden="true">⌄</i></button>' +
    '<div id="next-mic-options" class="next-mic-options" role="radiogroup" aria-labelledby="next-mic-heading" hidden></div>';
  host.append(mic);
  const current = mic.querySelector('button');
  const choices = mic.querySelector('.next-mic-options');
  let expanded = false, lastView = '';
  function paintMicrophone() {
    const { options, selection } = microphone.view;
    mic.hidden = options.length === 0;
    if (mic.hidden) expanded = false;
    const signature = JSON.stringify([options, selection, expanded]);
    if (lastView === signature) return;
    lastView = signature;
    const label = options.find(option => option.id === selection)?.label || 'System default';
    current.firstElementChild.textContent = label;
    current.setAttribute('aria-label', 'Microphone: ' + label);
    current.setAttribute('aria-expanded', String(expanded));
    choices.hidden = !expanded;
    choices.replaceChildren();
    if (!expanded) return;
    for (const option of options) {
      const b = doc.createElement('button');
      b.type = 'button';
      b.textContent = option.label;
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(option.id === selection));
      b.onclick = () => { expanded = false; microphone.choose(option.id); paintMicrophone(); current.focus(); };
      choices.append(b);
    }
  }
  current.onclick = () => { expanded = !expanded; paintMicrophone(); };
  paintMicrophone();

  return { paintMicrophone, destroy() { reading.guide(false); block.remove(); mic.remove(); } };
}

module.exports = { mountSettingsFields };
