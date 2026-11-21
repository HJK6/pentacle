'use strict';

const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');

const readScenario = name => readFileSync(resolve(__dirname, 'e2e/lib', name), 'utf8');
const e6 = readScenario('next_lanes_e6_scenario.js');
const e7 = readScenario('next_lanes_phone_scenario.js');

test('E6 proves Tab plus Space, exact link targets, and successful clicks', () => {
  assert.match(e6, /press\(ctx,\s*' ',\s*'Space',\s*32\)/);
  assert.match(e6, /focusPredecessorThenTab\(ctx, goalSelector, laneSelector, 'unique goal name', 'Space'\)/);
  assert.match(e6, /links\.length === 2/);
  assert.match(e6, /rendered\.ambiguousLinks === 0/);
  assert.match(e6, /async function clickAndCheck\(/);
  assert.match(e6, /result === true/);
  assert.match(e6, /await clickAndCheck\(ctx, laneSelector/);
  assert.match(e6, /await clickAndCheck\(ctx, goalSelector/);
  assert.match(e6, /await clickAndCheck\(ctx, `\$\{updateSelector\}\[data-update-id=/);
});

test('E7 proves root stage fit, exact live question card, derived rail geometry, and touch limits', () => {
  assert.match(e7, /E7 all-lanes root view fills the available phone stage/);
  assert.match(e7, /async function verifyPagerLabelsAgainstRenderedOrder\(/);
  assert.match(e7, /async function questionListOrder\(/);
  assert.match(e7, /const renderedOrder = questionOrder\.filter/);
  assert.match(e7, /const expectedNextId = renderedOrder\[/);
  assert.match(e7, /next\.id === expectedNextId && next\.label === expectedLabel/);
  assert.match(e7, /const expectedLabel = level === 'spec' \? 'Needs your answer' : pager\.selectedLabel/);
  assert.doesNotMatch(e7, /acceptedQuestionIds\.indexOf\(questionId\)/);
  assert.match(e7, /card\.label === expectedLabel/);
  assert.match(e7, /card\.mark === '\?'/);
  assert.match(e7, /card\.about === expectedAbout/);
  assert.match(e7, /card\.hasForm === true && card\.hasSent === false/);
  assert.doesNotMatch(e7, /next-question-(?:lane|goal)-title/);
  assert.match(e7, /\.next-lanes-lane-title/);
  assert.match(e7, /\.next-lanes-goal-title/);
  assert.match(e7, /const expectedAbout = level === 'spec' \? null/);
  assert.match(e7, /level === 'lane' \? `\$\{lane\.goals\[0\]\.title\} · \$\{member\.title\}` : member\.title/);
  assert.match(e7, /const railHeight = rail\?\.height/);
  assert.match(e7, /geometry\.viewport\?\.width === PHONE_WIDTH/);
  assert.match(e7, /near\(stage\?\.width, PHONE_WIDTH\)/);
  assert.match(e7, /stage\?\.height, viewportHeight - railHeight/);
  assert.match(e7, /E7 phone touch coverage bound/);
  assert.match(e7, /!card\.querySelector\('\.next-question-sent'\)/);
});
