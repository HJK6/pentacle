'use strict';

const VERSION = 1;
const VIEWS = new Set(['assistant', 'lanes', 'dashboards', 'chats', 'questions']);

function createShellBridge(transport, { navigate, repaint, nextFrame }) {
  if (transport?.version !== VERSION) return null;

  let inventory = false;
  let ready = false;
  let showing = false;
  let presentation = 0;
  let counts = { chats: 0, assistant: 0, questions: 0 };

  function endShowing() {
    showing = false;
    return ++presentation;
  }

  return {
    bind() {
      transport.onNavigate(view => {
        endShowing();
        if (VIEWS.has(view)) navigate(view);
      });
      transport.onShowing(visible => {
        const request = endShowing();
        if (!visible) return;
        // A later hide or navigation can arrive before the new view paints.
        nextFrame(() => {
          if (request !== presentation) return;
          showing = true;
          repaint();
        });
      });
    },
    receiveInventory() { inventory = true; },
    publish(update) {
      const next = { ...counts, ...update };
      const changed = next.chats !== counts.chats || next.assistant !== counts.assistant || next.questions !== counts.questions;
      counts = next;
      if (!ready && inventory && update.chats !== undefined) {
        ready = true;
        transport.ready({ version: VERSION, ...counts });
      } else if (ready && changed) {
        transport.setAttention(counts);
      }
    },
    canMarkSeen(hidden, popover) {
      return ready && showing && !hidden && popover !== 'chats' && popover !== 'questions';
    },
  };
}

module.exports = { createShellBridge };
