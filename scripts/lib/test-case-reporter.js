'use strict';
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const { junit } = require('node:test/reporters');

// Node emits completed cases and their enclosing suites in depth-first order.
// Keep the ancestry as an array so equal space-joined names stay distinguishable.
async function* observe(events, completion, receipt) {
  const stack = [];
  const cases = [];
  let summary, fileSummary, incomplete = false;
  for await (const event of events) {
    const {type, data} = event;
    if (type === 'test:start') {
      if (data.nesting !== stack.length) incomplete = true;
      stack.push(data.name);
    } else if (type === 'test:pass' || type === 'test:fail') {
      if (data.nesting !== stack.length - 1 || stack.at(-1) !== data.name) {
        incomplete = true;
      }
      const failure = data.details.error?.failureType;
      if (type === 'test:fail' && !['testCodeFailure', 'subtestsFailed'].includes(failure)) {
        incomplete = true;
      }
      const entry = {names: [...stack], skipped: !!(data.skip || data.todo)};
      if (failure === 'testCodeFailure' && !entry.skipped) {
        entry.assertion_digest = createHash('sha256').update(data.details.error.message).digest('hex');
        if (receipt) {
          completion.failures.push(entry);
          fs.writeFileSync(receipt, JSON.stringify({failures: completion.failures}));
        }
      }
      cases.push(entry);
      stack.pop();
    } else if (type === 'test:summary') {
      if (data.file) fileSummary = data.counts;
      else summary = data.counts;
    }
    yield event;
  }
  // A worker can exit zero before reporting and Node emits a passing file
  // wrapper. A completed registration stream has its own per-file summary.
  if (incomplete || stack.length || !fileSummary || !summary || fileSummary.cancelled || summary.cancelled ||
      cases.length !== fileSummary.tests + fileSummary.suites || cases.length !== summary.tests + summary.suites) {
    return;
  }
  completion.cases = cases;
}

module.exports = async function* (events) {
  const receipt = process.env.PENTACLE_TEST_COMPLETION_PATH;
  const completion = {failures: []};
  const observed = observe(events, completion, receipt);
  // Let native JUnit finish even when this invocation cannot prove completion.
  if (receipt) yield* junit(observed);
  else for await (const _ of observed) { /* Drain the events for collection. */ }
  if (!completion.cases) return;
  const result = JSON.stringify(completion.cases) + '\n';
  if (receipt) fs.writeFileSync(receipt, result);
  else yield result;
};
