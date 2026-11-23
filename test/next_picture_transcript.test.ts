import assert from 'node:assert/strict';
import test from 'node:test';
import type { PentacleTranscriptItem } from 'pentacle-chat-core';
import { renderTranscriptItemHtml } from '../renderer/src/shared_transcript_view';

const { JSDOM } = require('jsdom');

test('new transcript marks ordinary images by occurrence without changing legacy markup or plain links', () => {
  const item = {
    id: 'event:1', displayRule: 'bubble:assistant', text: '[Outside](https://example.invalid/picture) and [Designs](/designs/)',
    attachments: [
      { key: '9'.repeat(64), mime: 'image/png', filename: 'fixture/angle & view.png', width: 420, height: 150 },
      { key: '9'.repeat(64), mime: 'image/png', filename: 'same bytes.png' },
      { key: '8'.repeat(64), mime: 'application/pdf', filename: 'file.pdf', bytes: 4 },
    ],
  } as unknown as PentacleTranscriptItem;
  const parse = (pictureCards: boolean) => new JSDOM(renderTranscriptItemHtml(item, undefined, { pictureCards })).window.document;
  const next = parse(true);
  const legacy = parse(false);
  const thumbs = [...next.querySelectorAll('.slot-chat-media-button')] as HTMLElement[];
  assert.deepEqual(thumbs.map(t => t.getAttribute('aria-label')), ['angle & view.png', 'same bytes.png']);
  assert.notEqual(thumbs[0].dataset.nextPicture, thumbs[1].dataset.nextPicture);
  assert.equal(thumbs[0].getAttribute('aria-pressed'), 'false');
  assert.equal(legacy.querySelector('[data-next-picture]'), null);
  assert.equal(legacy.querySelector('.slot-chat-media-button').getAttribute('aria-label'), 'Image attachment 1');
  assert.equal(next.querySelector('a.slot-chat-file-download').download, 'file.pdf');
  for (const address of ['https://example.invalid/picture', '/designs/']) {
    const link = next.querySelector(`a[href="${address}"]`);
    assert.equal(link.target, '_blank');
    assert.equal(link.rel, 'noopener noreferrer');
    assert.equal(link.hasAttribute('data-next-design-link'), false);
  }
});
