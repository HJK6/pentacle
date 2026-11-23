'use strict';

const PENCIL = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m4 16-1 5 5-1L21 7l-4-4zM14 6l4 4"/></svg>';
const TRASH = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13m-8-9v6m4-6v6"/></svg>';

function mountChatList(host, { choose, rename, remove }) {
  const doc = host.ownerDocument;
  const make = (tag, cls, text) => {
    const node = doc.createElement(tag);
    node.className = cls;
    if (text) node.textContent = text;
    return node;
  };
  const button = (cls, label, icon) => {
    const b = make('button', cls, icon ? '' : label);
    b.type = 'button';
    if (icon) { b.innerHTML = icon; b.setAttribute('aria-label', label); b.title = label; }
    return b;
  };
  const rows = new Map();
  let entries = [];
  let editing = null;
  let asking = null;
  let mounted = true;

  const newButton = button('next-chat-new', 'New chat');
  newButton.setAttribute('aria-label', 'New chat');
  newButton.onclick = () => choose(null);
  const empty = make('div', 'empty', 'No chats yet.');
  host.append(newButton, empty);

  function restoreFocus(id, selector) { rows.get(id)?.element.querySelector(selector)?.focus(); }
  function finish(edit, save, restore) {
    if (editing !== edit) return; // Escape may cause a subsequent blur.
    const name = edit.input.value.trim();
    editing = null;
    if (save && name && name !== edit.name) void rename(edit.id, name);
    paint();
    if (restore) restoreFocus(edit.id, '.next-chat-rename');
  }

  function build(node, entry) {
    const {id, name, working, unread, selected, protectedRow, pending} = entry;
    node.className = 'next-chat-row' + (selected ? ' selected' : '') + (unread ? ' unread' : '');
    node.replaceChildren();
    if (editing?.id === id) {
      const edit = editing;
      node.append(edit.input);
      return;
    }
    if (asking === id) {
      const question = make('span', 'next-chat-question', 'Delete chat?');
      const cancel = button('next-chat-cancel', 'Cancel');
      const confirm = button('next-chat-confirm', 'Delete');
      cancel.onclick = () => { asking = null; paint(); restoreFocus(id, '.next-chat-delete'); };
      confirm.onclick = () => {
        asking = null;
        void remove(id);
        paint();
        restoreFocus(id, '.next-chat-pick');
      };
      node.append(question, cancel, confirm);
      return;
    }
    const select = button('next-chat-pick', name);
    select.dataset.streamId = id; // existing row-selection hook
    select.setAttribute('aria-current', selected ? 'true' : 'false');
    const lead = make('span', 'next-chat-lead');
    lead.setAttribute('aria-hidden', 'true');
    if (working) lead.append(make('i', 'next-ring'));
    select.replaceChildren(lead, make('span', 'next-chat-name', name));
    select.onclick = () => choose(id);
    node.append(select);
    if (unread) {
      const dot = make('i', 'next-chat-dot');
      dot.setAttribute('role', 'img');
      dot.setAttribute('aria-label', 'Unread');
      node.append(dot);
    }
    if (protectedRow) return;
    const actions = make('span', 'next-chat-actions');
    const change = button('next-chat-rename', 'Rename', PENCIL);
    const trash = button('next-chat-delete', 'Delete', TRASH);
    change.disabled = trash.disabled = pending;
    change.onclick = () => {
      asking = null;
      const input = make('input', 'next-chat-edit');
      input.type = 'text';
      input.value = name;
      input.spellcheck = false;
      input.autocomplete = 'off';
      input.setAttribute('aria-label', 'Chat name');
      const edit = editing = {id, name, input};
      input.addEventListener('blur', () => finish(edit, true, false));
      input.addEventListener('keydown', event => {
        if (event.isComposing || !['Enter', 'Escape'].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        finish(edit, event.key === 'Enter', true);
      });
      paint();
      input.focus(); input.select();
    };
    trash.onclick = () => { asking = id; paint(); restoreFocus(id, '.next-chat-cancel'); };
    actions.append(change, trash);
    node.append(actions);
  }

  function paint() {
    if (!mounted) return;
    const ids = new Set(entries.map(e => e.id));
    if (editing && !ids.has(editing.id)) editing = null;
    if (asking && !ids.has(asking)) asking = null;
    for (const [id, row] of rows) if (!ids.has(id)) { row.element.remove(); rows.delete(id); }
    let previous = empty;
    for (const entry of entries) {
      let row = rows.get(entry.id);
      if (!row) {
        row = {element: make('div', 'next-chat-row'), signature: ''};
        rows.set(entry.id, row);
      }
      const signature = JSON.stringify(editing?.id === entry.id ? ['editing'] : [entry, asking === entry.id]);
      const focused = row.element.contains(doc.activeElement);
      if (row.signature !== signature) {
        const focusClass = focused ? doc.activeElement.className : null;
        row.signature = signature;
        build(row.element, entry);
        if (focusClass && !editing && doc.activeElement === doc.body)
          Array.from(row.element.querySelectorAll('button')).find(b => b.className === focusClass)?.focus({preventScroll: true});
      }
      // Do not detach a focused input on an inventory-driven reorder.
      if (!focused && previous.nextSibling !== row.element) previous.after(row.element);
      previous = row.element;
    }
    empty.hidden = entries.length > 0;
  }
  return {
    update(value) { entries = value; paint(); },
    escape() {
      if (editing) { finish(editing, false, true); return true; }
      return false;
    },
    destroy() { mounted = false; editing = asking = null; host.replaceChildren(); },
  };
}

module.exports = { mountChatList };
