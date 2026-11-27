'use strict';

// A page knows the build it was loaded from, and the host says which build it
// serves now. When both are known and differ, a newer version is waiting for a
// reload. A failed question keeps the last answer; the next check asks again.
function createUpdateCheck({ ownId, getBuild, onChange }) {
  let available = false;
  return {
    available: () => available,
    async check() {
      if (!ownId) return available;
      let servedId;
      try { servedId = (await getBuild())?.buildId; } catch { return available; }
      const next = !!servedId && servedId !== ownId;
      if (next !== available) { available = next; onChange(available); }
      return available;
    },
  };
}

module.exports = { createUpdateCheck };
