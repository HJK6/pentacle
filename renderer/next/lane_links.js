'use strict';

// Text carries no stable id; only unambiguous exact titles may become links.
function uniqueTargets(targets) {
  const byTitle = new Map();
  for (const target of targets || []) {
    const title = typeof target.title === 'string' ? target.title.trim() : '';
    if (!title || !target.laneId) continue;
    if (!byTitle.has(title)) byTitle.set(title, []);
    const entries = byTitle.get(title);
    if (!entries.some(entry => entry.laneId === target.laneId && (entry.goalId || '') === (target.goalId || ''))) entries.push(target);
  }
  return [...byTitle].filter(([, entries]) => entries.length === 1)
    .map(([title, entries]) => ({ ...entries[0], title }))
    .sort((a, b) => b.title.length - a.title.length || a.title.localeCompare(b.title));
}
const word = c => !!c && /[\p{L}\p{N}_]/u.test(c);
function findNames(text, targets, used = new Set()) {
  const found = [];
  for (let index = 0; index < text.length;) {
    let match = null;
    for (const target of targets) {
      if (used.has(target.title) || !text.startsWith(target.title, index)) continue;
      const before = index ? text[index - 1] : '';
      const after = text[index + target.title.length] || '';
      if ((word(before) && word(target.title[0])) || (word(after) && word(target.title.at(-1)))) continue;
      match = target; break;
    }
    if (!match) { index++; continue; }
    found.push({ start: index, end: index + match.title.length, target: match });
    used.add(match.title); index += match.title.length;
  }
  return found;
}
function linkAssistantNames(root, targets) {
  const choices = uniqueTargets(targets); if (!root || !choices.length) return;
  for (const message of root.querySelectorAll('.slot-chat-assistant-card')) {
    const used = new Set(); const walker = message.ownerDocument.createTreeWalker(message, 4);
    const textNodes = []; while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const textNode of textNodes) {
      if (textNode.parentElement?.closest('a, button, code, pre, script, style, .next-lane-name-link')) continue;
      const text = textNode.nodeValue || ''; const matches = findNames(text, choices, used); if (!matches.length) continue;
      const fragment = message.ownerDocument.createDocumentFragment(); let cursor = 0;
      for (const match of matches) {
        fragment.append(message.ownerDocument.createTextNode(text.slice(cursor, match.start)));
        const link = message.ownerDocument.createElement('button'); link.type = 'button'; link.className = 'next-lane-name-link';
        link.dataset.laneId = match.target.laneId; if (match.target.goalId) link.dataset.goalId = match.target.goalId;
        link.textContent = text.slice(match.start, match.end); fragment.append(link); cursor = match.end;
      }
      fragment.append(message.ownerDocument.createTextNode(text.slice(cursor))); textNode.replaceWith(fragment);
    }
  }
}
module.exports = { uniqueTargets, findNames, linkAssistantNames };
