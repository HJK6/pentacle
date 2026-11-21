'use strict';
function revisionRequestsCoalesce(calls, laneId, revisionA, revisionB) {
  const rows = calls.filter(call => call.laneId === laneId);
  return rows.some(call => call.membersLimit === 32 && call.revision === revisionA)
    && rows.some(call => call.membersLimit === 32 && call.revision === revisionB)
    && !rows.some(call => call.membersLimit === 1 && call.revision === revisionB);
}
module.exports = { revisionRequestsCoalesce };
