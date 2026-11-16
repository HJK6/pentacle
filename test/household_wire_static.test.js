'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { FakeHousehold } = require('./fixtures/household_support');
const FILES = ['selectors.js', 'store.js'];
const allowed = new Map([
  ['household.snapshot', ['month']], ['household.item.add', ['list','label']],
  ['household.item.done', ['item_id']], ['household.item.remove', ['item_id']],
  ['household.event.add', ['date','time','title','who']], ['household.event.remove', ['event_id']],
]);
function violations(source) {
  const problems=[];
  if (/\btodo\.|\bv2_todo\b/.test(source)) problems.push('forbidden legacy verb');
  if (/\b(?:localStorage|sessionStorage|indexedDB)\b/.test(source)) problems.push('persistent household cache');
  // Outbound calls must name a literal verb and construct an explicit object. The one
  // household(verb, fields) forwarding boundary is checked separately below.
  const calls = [...source.matchAll(/\brequest\(\s*['"]([^'"]+)['"]\s*,\s*([\s\S]*?)\)\s*[,;]/g)];
  for (const [,verb,payload] of calls) {
    if (!allowed.has(verb)) { problems.push(`unknown verb ${verb}`); continue; }
    if (/\.\.\./.test(payload)) problems.push(`spread in ${verb}`);
    if (!payload.includes('{')) problems.push(`nonliteral fields in ${verb}`);
    const forbidden = payload.match(/\b(?:scope|priority|due_date|created_by|position)\s*[:},]/g);
    if (forbidden) problems.push(`forbidden fields in ${verb}`);
    const keys = [...payload.matchAll(/(?:\{|,)\s*([a-z_]+)\s*(?=[:,}])/g)].map(m=>m[1]);
    if (keys.some(key => !allowed.get(verb).includes(key))) problems.push(`extra field in ${verb}`);
  }
  for(const match of source.matchAll(/\bhousehold\(\s*([^,]+),\s*([^)]*)\)/g)) {
    if (match[1].trim() !== 'verb' || match[2].trim() !== 'fields') problems.push('direct household payload outside named wire boundary');
  }
  return problems;
}
test('static household wire guard covers every public household helper',()=>{
  for(const filename of FILES) {
    const full=path.join(__dirname,'../renderer/household',filename);
    assert.ok(fs.existsSync(full),`Missing public household helper ${filename}`);
    assert.deepEqual(violations(fs.readFileSync(full,'utf8')),[],filename);
  }
});
test('wire boundary constructs all six literal verbs and never spreads caller fields',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../renderer/household/store.js'),'utf8');
  assert.deepEqual([...new Set([...source.matchAll(/\brequest\(\s*['"]([^'"]+)['"]/g)].map(m=>m[1]))].sort(),[...allowed.keys()].sort());
  assert.equal([...source.matchAll(/await household\(verb, fields\)/g)].length,1);
  assert.equal([...source.matchAll(/\bhousehold\(/g)].length,1);
});
for(const field of ['scope','priority','due_date','created_by','position']) test(`wire guard rejects injected ${field}`,()=>{
  assert.ok(violations(`request('household.item.add', { list, label, ${field}: 'poison' });`).length);
});
for(const payload of ["request('todo.add', { label });", "request('household.item.add', { list, label, unexpected: true });", "request('household.event.add', { ...value });", "household('household.item.add', { label, scope: 'shared' });", 'v2_todo()', 'localStorage.setItem("rows", value)']) test(`wire negative control ${payload}`,()=>assert.ok(violations(payload).length));

const validPayloads = [
  ['household.snapshot', {}], ['household.snapshot', { month: '2054-10' }],
  ['household.item.add', { list: 'tasks', label: 'Synthetic item' }],
  ['household.item.done', { item_id: 1 }], ['household.item.remove', { item_id: 1 }],
  ['household.event.add', { date: '2054-10-06', time: null, title: 'Synthetic event', who: 'self' }],
  ['household.event.remove', { event_id: 101 }],
];
for(const [verb, fields] of validPayloads) test(`runtime wire fixture accepts exact ${verb} fields ${Object.keys(fields)}`,async()=>{
  const server = new FakeHousehold();
  assert.equal((await server.handle(verb, fields)).ok, true);
  assert.deepEqual(server.calls, [{ verb, fields }]);
});
for(const [verb, fields] of validPayloads) {
  for(const field of ['scope','priority','due_date','created_by','position','unexpected']) test(`runtime wire fixture rejects ${verb} extra ${field} before mutation`,async()=>{
    const server = new FakeHousehold(), before = structuredClone(server.snapshot);
    await assert.rejects(server.handle(verb, { ...fields, [field]: 'poison' }), /Unexpected .* fields/);
    assert.deepEqual(server.calls, []);
    assert.deepEqual(server.snapshot, before);
  });
}
test('runtime wire fixture rejects unknown verb before mutation',async()=>{
  const server = new FakeHousehold(), before = structuredClone(server.snapshot);
  await assert.rejects(server.handle('example.unsupported', {}), /Unexpected verb/);
  assert.deepEqual(server.calls, []);
  assert.deepEqual(server.snapshot, before);
});
