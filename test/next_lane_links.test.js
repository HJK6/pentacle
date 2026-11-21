'use strict';
const test = require('node:test'); const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { uniqueTargets, findNames, linkAssistantNames } = require('../renderer/next/lane_links');
const targets = [{title:'Alpha',laneId:'one'}, {title:'Alpha Road',laneId:'one',goalId:'road'}, {title:'Shared',laneId:'one'}, {title:'Shared',laneId:'two'}];
test('matches longest exact whole phrases only once and ignores ambiguous titles', () => {
 const choices=uniqueTargets(targets); const m=findNames('Alpha Road, Alpha; Alpha Shared Alphabet alpha',choices);
 assert.deepEqual(m.map(x=>x.target.title), ['Alpha Road','Alpha']);
 assert.equal(uniqueTargets([targets[0], {...targets[0]}]).length,1);
});
test('only assistant text becomes keyboard-accessible links, never code or existing anchors', () => {
 const dom=new JSDOM('<div id="root"><article class="slot-chat-row is-user">Alpha</article><div class="slot-chat-assistant-card"><p>Alpha Road and Alpha Road; Alpha</p><code>Alpha</code><a href="https://example.test">Alpha</a></div><div class="slot-chat-assistant-card">Alpha</div></div>');
 const root=dom.window.document.querySelector('#root'); linkAssistantNames(root,targets);
 const links=[...root.querySelectorAll('button.next-lane-name-link')];
 assert.deepEqual(links.map(x=>x.textContent),['Alpha Road','Alpha','Alpha']);
 assert.equal(links[0].dataset.goalId,'road'); assert.equal(links[1].type,'button');
 assert.equal(root.querySelector('code').textContent,'Alpha'); assert.equal(root.querySelector('a').textContent,'Alpha');
 assert.equal(root.querySelector('.is-user button'),null);
});
