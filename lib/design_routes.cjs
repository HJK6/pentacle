'use strict';

const PREFIX = /^\/next\/(embed\/)?design(?=\/|$)/;
// Project, design and view IDs use the Design library's ID grammar.
const ID = /^[a-z0-9][a-z0-9-]{0,79}$/;

function parseDesignRoute(raw) {
  if (typeof raw !== 'string') return null;
  const prefix = PREFIX.exec(raw);
  if (!prefix) return null;

  const mode = prefix[1] ? 'embed' : 'workspace';
  const suffix = raw.slice(prefix[0].length);
  if (!suffix) return { mode, target: null };

  const ids = suffix.slice(1).split('/');
  if (ids.length < 2 || ids.length > 3 || ids.some(id => !ID.test(id))) return null;
  const [project_id, design_id, view_id] = ids;
  const target = { project_id, design_id };
  if (view_id !== undefined) target.view_id = view_id;
  return { mode, target };
}

module.exports = { parseDesignRoute };
