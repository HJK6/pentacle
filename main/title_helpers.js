'use strict';

function titleCase(words) {
  return String(words || '')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 7)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

function cleanTitleCandidate(raw) {
  let title = String(raw || '').trim();
  try {
    const parsed = JSON.parse(title);
    if (parsed && parsed.title) title = String(parsed.title);
  } catch {}
  title = title
    .replace(/^["'`]+|["'`]+$/g, '')
    .replace(/[.,:;!?()[\]{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!title || title.includes('-')) return '';
  if (/^(codex|claude)( code| chat)?$/i.test(title)) return '';
  if (/^(hosta|hostb|hostc|hostd)$/i.test(title)) return '';
  const words = title.split(/\s+/).filter(Boolean);
  if (words.length < 2 || words.length > 7) return '';
  if (/\d{6,}/.test(title)) return '';
  return titleCase(title);
}

module.exports = { titleCase, cleanTitleCandidate };
