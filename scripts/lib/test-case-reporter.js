'use strict';

// Node emits completed cases and their enclosing suites in depth-first order.
// Keep the ancestry as an array so equal space-joined names stay distinguishable.
module.exports = async function* (events) {
  const stack = [];
  const cases = [];
  let summary;
  for await (const {type, data} of events) {
    if (type === 'test:start') {
      if (data.nesting !== stack.length) throw new Error('incomplete Node test ancestry');
      stack.push(data.name);
    } else if (type === 'test:pass' || type === 'test:fail') {
      if (data.nesting !== stack.length - 1 || stack.at(-1) !== data.name) {
        throw new Error('incomplete Node test result');
      }
      cases.push({
        names: [...stack],
        failure: type === 'test:fail' ? data.details.error?.failureType || 'unknown' : null,
        skipped: !!(data.skip || data.todo),
      });
      stack.pop();
    } else if (type === 'test:summary' && !data.file) {
      summary = data.counts;
    }
  }
  if (stack.length || !summary || cases.length !== summary.tests + summary.suites) {
    throw new Error('incomplete Node test discovery');
  }
  yield JSON.stringify(cases) + '\n';
};
