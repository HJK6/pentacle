const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');

function readRepoFile(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), 'utf8');
}

function functionBody(source, name) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} should exist`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(open + 1, i);
  }
  throw new Error(`could not parse ${name}`);
}

test('desktop transcript summarizer entry points are retired', () => {
  const main = readRepoFile('main.js') + readRepoFile('main/cc_handlers.js');
  const renderer = readRepoFile('renderer/app.js');
  const preload = readRepoFile('preload.js');

  assert.doesNotMatch(main, /generateTitleWithCodex/);
  assert.doesNotMatch(main, /tmux:maybe-title-session/);
  assert.doesNotMatch(main, /heuristicTitleFromTranscript|buildTitlePrompt|llmAttempts/);
  assert.doesNotMatch(renderer, /maybeTitleSession/);
  assert.doesNotMatch(preload, /maybeTitleSession/);
});

test('session creation no longer writes the New Chat tmux title', () => {
  const main = readRepoFile('main/cc_handlers.js');
  const body = main.slice(main.indexOf("target.handle('chat-stream:spawn'"), main.indexOf("target.handle('chat-stream:send'"));

  assert.doesNotMatch(body, /renameTmuxWindow/);
  assert.doesNotMatch(body, /New Chat/);
  assert.match(body, /chatStreamClient.spawnSession/);
});

test('desktop chat manual rename sends the manual source flag', () => {
  const main = readRepoFile('main/cc_handlers.js');
  const renameHandler = main.slice(main.indexOf("target.handle('chat-stream:rename'"));

  assert.match(renameHandler, /renameSession\(\{/);
  assert.match(renameHandler, /source:\s*'manual'/);
});
