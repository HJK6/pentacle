const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { CdpSession } = require('./lib/cdp');

test('diagnostic readers retain original entry fields and exact string text; exceptions add entries', async () => {
  const ws = new EventEmitter();
  const session = new CdpSession(ws, {});
  const text = '  [HARNESS] {"seq":1,"name":"é🧪"}\n  ';
  ws.emit('message', JSON.stringify({method:'Runtime.consoleAPICalled',params:{type:'log',args:[{type:'string',value:text}]}}));
  assert.equal(session.consoleLines[0].text, text);
  const before = {...session.consoleLines[0]};
  ws.emit('message', JSON.stringify({method:'Runtime.exceptionThrown',params:{exceptionDetails:{text:'Uncaught',url:'fixture',lineNumber:12,exception:{description:'Error: fixture'}}}}));
  await session.drainConsole();
  assert.deepEqual(Object.keys(session.consoleLines[0]), ['ts','type','text']);
  assert.deepEqual(session.consoleLines[0], before);
  assert.deepEqual(Buffer.from(session.consoleLines[0].text), Buffer.from(text));
  assert.equal(session.consoleLines.length, 2);
  assert.equal(session.consoleLines[1].type, 'exception');
  assert.equal(session.consoleLines[1].line, 12);
});
