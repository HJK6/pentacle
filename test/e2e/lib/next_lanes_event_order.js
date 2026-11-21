'use strict';
function outcomeBeforeRead(timeline, outcomeType, questionId, readSequence) {
  const outcomeIndex = timeline.findIndex(row => row.type === outcomeType && row.questionId === questionId);
  const readIndex = timeline.findIndex(row => row.type === 'prompt.list:start' && row.sequence === readSequence);
  return outcomeIndex >= 0 && readIndex > outcomeIndex
    && Number.isFinite(timeline[outcomeIndex].at) && Number.isFinite(timeline[readIndex].at)
    && timeline[outcomeIndex].at <= timeline[readIndex].at;
}
function resolveBeforeOutcome(timeline, outcomeType, questionId) {
  const start = timeline.findIndex(row => row.type === 'notification.resolve:start' && row.questionId === questionId);
  const end = timeline.findIndex(row => row.type === outcomeType && row.questionId === questionId);
  return start >= 0 && end > start && Number.isFinite(timeline[start].at)
    && Number.isFinite(timeline[end].at) && timeline[start].at <= timeline[end].at;
}
module.exports = { outcomeBeforeRead, resolveBeforeOutcome };
