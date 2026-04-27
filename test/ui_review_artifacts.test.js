'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
  artifactKey,
  listLocalArtifacts,
  normalizeHubIndex,
  safeBundleEntry,
  slug,
} = require('../main/ui_review_artifacts');

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-ui-review-'));
}

test('slug and artifactKey normalize machine-safe identifiers', () => {
  assert.equal(slug(' Pentacle Mobile / Chat '), 'pentacle-mobile-chat');
  assert.equal(artifactKey('Pentacle Mobile', 'Mobile Chat/Options', 'Merlin'), 'pentacle-mobile/mobile-chat-options/merlin');
  assert.equal(artifactKey('', 'x', 'merlin'), null);
});

test('safeBundleEntry rejects path traversal and absolute paths', () => {
  const root = tempRoot();
  fs.writeFileSync(path.join(root, 'index.html'), '<html></html>');

  assert.equal(safeBundleEntry(root, 'index.html'), path.join(root, 'index.html'));
  assert.equal(safeBundleEntry(root, '../secret.html'), null);
  assert.equal(safeBundleEntry(root, '/tmp/secret.html'), null);
});

test('listLocalArtifacts reads manifest bundles and legacy HTML fallback', () => {
  const root = tempRoot();
  const repo = path.join(root, 'pentacle-mobile');
  const reviewDir = path.join(repo, '.ui-review', 'mobile-chat-options');
  const legacyDir = path.join(repo, 'test', 'artifacts');
  fs.mkdirSync(reviewDir, { recursive: true });
  fs.mkdirSync(legacyDir, { recursive: true });
  fs.writeFileSync(path.join(reviewDir, 'index.html'), '<title>Options</title>');
  fs.writeFileSync(path.join(reviewDir, 'manifest.json'), JSON.stringify({
    schema: 'pentacle.uiReview.v1',
    id: 'mobile-chat-options',
    repo: 'pentacle-mobile',
    title: 'Mobile Chat Options',
    entry: 'index.html',
    createdAt: '2026-04-27T00:00:00Z',
    tags: ['mobile', 'chat'],
    extraField: 'preserve me',
  }));
  fs.writeFileSync(path.join(legacyDir, 'legacy.html'), '<title>Legacy QA</title>');

  const result = listLocalArtifacts({ repoRoots: [root], artifactDirs: [] }, 'Merlin');
  assert.equal(result.source, 'local-fallback');
  assert.equal(result.artifacts.length, 2);
  const manifestArtifact = result.artifacts.find((item) => item.artifactKey === 'pentacle-mobile/mobile-chat-options/merlin');
  assert.ok(manifestArtifact);
  assert.equal(manifestArtifact.source.manifest.extraField, 'preserve me');
  assert.ok(result.artifacts.some((item) => item.title === 'Legacy QA'));
});

test('listLocalArtifacts skips unsafe manifests without blocking valid artifacts', () => {
  const root = tempRoot();
  const repo = path.join(root, 'demo-repo');
  const valid = path.join(repo, '.ui-review', 'valid');
  const invalid = path.join(repo, '.ui-review', 'invalid');
  fs.mkdirSync(valid, { recursive: true });
  fs.mkdirSync(invalid, { recursive: true });
  fs.writeFileSync(path.join(valid, 'index.html'), '<html></html>');
  fs.writeFileSync(path.join(valid, 'manifest.json'), JSON.stringify({
    schema: 'pentacle.uiReview.v1',
    id: 'valid',
    repo: 'demo-repo',
    title: 'Valid',
    entry: 'index.html',
    createdAt: '2026-04-27T00:00:00Z',
  }));
  fs.writeFileSync(path.join(invalid, 'manifest.json'), JSON.stringify({
    schema: 'pentacle.uiReview.v1',
    id: 'invalid',
    repo: 'demo-repo',
    title: 'Invalid',
    entry: '../escape.html',
  }));

  const result = listLocalArtifacts({ repoRoots: [root], artifactDirs: [] }, 'Merlin');
  assert.deepEqual(result.artifacts.map((item) => item.id), ['valid']);
});

test('listLocalArtifacts rejects missing createdAt and symlink entries', () => {
  const root = tempRoot();
  const repo = path.join(root, 'demo-repo');
  const missingCreatedAt = path.join(repo, '.ui-review', 'missing-created-at');
  const symlinkEntry = path.join(repo, '.ui-review', 'symlink-entry');
  fs.mkdirSync(missingCreatedAt, { recursive: true });
  fs.mkdirSync(symlinkEntry, { recursive: true });
  fs.writeFileSync(path.join(missingCreatedAt, 'index.html'), '<html></html>');
  fs.writeFileSync(path.join(missingCreatedAt, 'manifest.json'), JSON.stringify({
    schema: 'pentacle.uiReview.v1',
    id: 'missing-created-at',
    repo: 'demo-repo',
    title: 'Missing createdAt',
    entry: 'index.html',
  }));
  fs.writeFileSync(path.join(symlinkEntry, 'real.html'), '<html></html>');
  fs.symlinkSync(path.join(symlinkEntry, 'real.html'), path.join(symlinkEntry, 'index.html'));
  fs.writeFileSync(path.join(symlinkEntry, 'manifest.json'), JSON.stringify({
    schema: 'pentacle.uiReview.v1',
    id: 'symlink-entry',
    repo: 'demo-repo',
    title: 'Symlink entry',
    entry: 'index.html',
    createdAt: '2026-04-27T00:00:00Z',
  }));

  const result = listLocalArtifacts({ repoRoots: [root], artifactDirs: [] }, 'Merlin');
  assert.deepEqual(result.artifacts, []);
});

test('normalizeHubIndex sorts artifacts and adds staleness metadata', () => {
  const now = Date.now();
  const env = {
    updated_at: new Date(now - 1000).toISOString(),
    server_received_at: new Date(now - 600000).toISOString(),
    freshness_ttl_sec: 60,
    data: {
      schema: 'pentacle.uiReviewIndex.v1',
      artifacts: [
        { id: 'old', repo: 'demo', machine: 'merlin', title: 'Old', entryUrl: 'http://hub/old', updatedAt: '2026-01-01T00:00:00Z' },
        { id: 'new', repo: 'demo', machine: 'merlin', title: 'New', entryUrl: 'http://hub/new', updatedAt: '2026-01-02T00:00:00Z' },
      ],
    },
  };

  const result = normalizeHubIndex(env, false);
  assert.equal(result.source, 'hub');
  assert.equal(result._transport_stale, true);
  assert.equal(result._data_stale, true);
  assert.equal(result.artifacts[0].id, 'new');
  assert.equal(result.artifacts[0].artifactKey, 'demo/new/merlin');
});

test('normalizeHubIndex reports malformed schema and skips invalid artifacts', () => {
  const malformed = normalizeHubIndex({
    updated_at: '2026-04-27T00:00:00Z',
    server_received_at: '2026-04-27T00:00:00Z',
    data: { schema: 'wrong', artifacts: [] },
  }, true);
  assert.match(malformed.error, /Malformed UI Review index/);

  const invalid = normalizeHubIndex({
    updated_at: '2026-04-27T00:00:00Z',
    server_received_at: '2026-04-27T00:00:00Z',
    data: {
      schema: 'pentacle.uiReviewIndex.v1',
      artifacts: [
        { id: 'bad', repo: 'demo', machine: 'merlin', entryUrl: 'http://hub/bad' },
        { id: 'good', repo: 'demo', machine: 'merlin', title: 'Good', entryUrl: 'http://hub/good', updatedAt: '2026-04-27T00:00:00Z' },
      ],
    },
  }, true);
  assert.equal(invalid.invalidArtifactCount, 1);
  assert.deepEqual(invalid.artifacts.map((item) => item.id), ['good']);
});
