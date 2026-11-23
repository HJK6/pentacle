'use strict';

const layout = require('./layout');
const selector = '.slot-chat-media-button[data-next-picture]';

function pictureFrom(transcript, button) {
  return {
    id: 'picture:' + JSON.stringify([transcript.dataset.streamId, button.dataset.nextPicture]),
    name: button.getAttribute('aria-label') || 'Picture',
  };
}

function markPictures(transcript, active) {
  for (const button of transcript.querySelectorAll('[data-next-picture]')) {
    button.setAttribute('aria-pressed', String(pictureFrom(transcript, button).id === active));
  }
}

function leavePicture(state, width) {
  if (!layout.isPicture(state.dash)) return state;
  return layout.shut({ ...state, peek: null, solo: null, last: 'assistant' }, 'dash', width);
}

function createPictureCard(card, { close, full }) {
  const doc = card.ownerDocument;
  const node = (tag, cls, parent) => {
    const el = doc.createElement(tag);
    el.className = cls;
    parent.append(el);
    return el;
  };
  const head = node('header', 'card-head', card);
  head.dataset.grab = 'dash';
  const title = node('div', 'card-title picture-title', head);
  head.append(full, close);
  const well = node('div', 'picture-well', card);
  const img = node('img', 'picture-image', well);
  const status = node('div', 'picture-status', well);
  status.setAttribute('role', 'status');
  let generation = 0;

  function clear() {
    generation++;
    img.onerror = null;
    img.removeAttribute('src');
    img.hidden = status.hidden = true;
  }

  async function open(picture, load) {
    clear();
    const current = generation;
    title.textContent = title.title = img.alt = picture.name;
    status.textContent = 'Loading picture…';
    status.hidden = false;
    const unavailable = () => {
      if (current !== generation) return;
      status.textContent = 'Picture unavailable';
      status.hidden = false;
      img.hidden = true;
    };
    try {
      const src = await load();
      if (current !== generation) return;
      img.onerror = unavailable;
      img.src = src;
      img.hidden = false;
      status.hidden = true;
    } catch {
      unavailable();
    }
  }

  return { open, close: clear };
}

module.exports = { selector, pictureFrom, markPictures, leavePicture, createPictureCard };
