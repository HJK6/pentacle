const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const vm = require('node:vm');
const { CdpSession } = require(process.env.CDP_EXTRACT_MODULE || './lib/cdp');

async function extract(value) {
  const session = new CdpSession(new EventEmitter(), {});
  session.send = async (_method, params) => {
    const fn = vm.runInNewContext(`(${params.functionDeclaration})`);
    return {result:{value:fn.call(value)}};
  };
  return session._consoleArg({type:'object',objectId:'fixture'});
}

test('callable toJSON cannot expose nested synthetic secrets', async () => {
  const token = 'synthetic-token';
  const source = {nested:{token, secret:'synthetic-secret', toJSON() { return {token, password:'synthetic-password'}; }}};
  const text = await extract(source);
  assert.doesNotMatch(text, /synthetic-(token|secret|password)/);
  const parsed = JSON.parse(text);
  assert.equal(parsed.nested.token, '[redacted]');
  assert.equal(parsed.nested.secret, '[redacted]');
});

test('dense arrays have a visible truncation marker within the byte cap', async () => {
  const text = await extract(Array(4000).fill(1));
  assert.ok(Buffer.byteLength(text) <= 2048);
  assert.match(text, /\[truncated\]/);
  assert.doesNotThrow(() => JSON.parse(text));
});

test('sparse arrays do not expand to a huge serialized array', async () => {
  const sparse = []; sparse[1000000] = 'end';
  const text = await extract(sparse);
  assert.ok(Buffer.byteLength(text) <= 2048);
  assert.match(text, /\[truncated\]/);
  assert.doesNotThrow(() => JSON.parse(text));
});

test('nested redaction, depth, cycles, and unavailable accessors remain explicit', async () => {
  const value = {nested:{token:'synthetic-token',secret:'synthetic-secret',password:'synthetic-password',dsn:'synthetic-dsn',authorization:'synthetic-auth',cookie:'synthetic-cookie'}, deep:{a:{b:{c:'too deep'}}}, zero:0, no:false, nil:null};
  value.self = value;
  Object.defineProperty(value, 'unavailable', {enumerable:true, get() { throw Error('must not execute'); }});
  const text = await extract(value);
  const parsed = JSON.parse(text);
  for (const key of ['token','secret','password','dsn','authorization','cookie']) assert.equal(parsed.nested[key], '[redacted]');
  assert.equal(parsed.self, '[cyclic]');
  assert.equal(parsed.deep.a.b, '[truncated]');
  assert.equal(parsed.unavailable, '[unavailable]');
  assert.equal(parsed.zero, 0);
  assert.equal(parsed.no, false);
  assert.equal(parsed.nil, null);
  assert.ok(Buffer.byteLength(text) <= 2048);
});

test('inherited serialization hooks cannot modify inert extracted containers', async () => {
  const session = new CdpSession(new EventEmitter(), {});
  session.send = async (_method, params) => {
    const context = vm.createContext({});
    vm.runInContext("Object.prototype.toJSON = () => ({token:'synthetic-token'}); Array.prototype.toJSON = () => ['synthetic-secret']", context);
    const fn = vm.runInContext(`(${params.functionDeclaration})`, context);
    return {result:{value:fn.call({items:[1,2], nested:{password:'synthetic-password'}})}};
  };
  const text = await session._consoleArg({type:'object',objectId:'fixture'});
  assert.doesNotMatch(text, /synthetic-(token|secret|password)/);
  assert.deepEqual(JSON.parse(text), {items:[1,2],nested:{password:'[redacted]'}});
});
