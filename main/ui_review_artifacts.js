'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const UI_REVIEW_DASHBOARD_ID = 'ui.review.index';

function expandHome(value) {
  if (!value || typeof value !== 'string') return value;
  if (value === '~') return os.homedir();
  if (value.startsWith('~/')) return path.join(os.homedir(), value.slice(2));
  return value;
}

function slug(value) {
  const result = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return result || null;
}

function artifactKey(repo, id, machine) {
  const parts = [slug(repo), slug(id), slug(machine)];
  return parts.some((part) => !part) ? null : parts.join('/');
}

function safeReadDir(dir) {
  try { return fs.readdirSync(dir, { withFileTypes: true }); } catch { return []; }
}

function readJson(filePath) {
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); } catch { return null; }
}

function htmlTitle(filePath) {
  try {
    const sample = fs.readFileSync(filePath, 'utf8').slice(0, 12000);
    const match = sample.match(/<title[^>]*>([^<]+)<\/title>/i)
      || sample.match(/<h1[^>]*>([^<]+)<\/h1>/i);
    return match ? match[1].trim().replace(/\s+/g, ' ') : '';
  } catch { return ''; }
}

function repoNameForArtifact(filePath) {
  return path.basename(path.dirname(filePath));
}

function reviewArtifactSearchDirs(config = {}) {
  const configured = Array.isArray(config.artifactDirs)
    ? config.artifactDirs.map(expandHome)
    : [path.join(process.cwd(), 'test', 'artifacts')];
  return Array.from(new Set(configured.filter((dir) => typeof dir === 'string' && dir)));
}

function safeBundleEntry(root, entry) {
  if (!entry || typeof entry !== 'string' || path.isAbsolute(entry)) return null;
  if (entry.split(/[\\/]+/).includes('..')) return null;
  const resolved = path.resolve(root, entry);
  const relative = path.relative(root, resolved);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return resolved;
}

function publicMachineLabel(value) {
  const raw = String(value || '').toLowerCase();
  if (raw === 'hosta' || raw === 'hostb' || raw === 'hostc' || raw === 'hostd') return raw;
  return 'hosta';
}

function manifestArtifact(manifestPath, machine) {
  const root = path.dirname(manifestPath);
  const manifest = readJson(manifestPath);
  if (!manifest || manifest.schema !== 'pentacle.uiReview.v1') return null;
  if (!manifest.repo || !manifest.id || !manifest.title || !manifest.entry || !manifest.createdAt) return null;
  const entryPath = safeBundleEntry(root, manifest.entry);
  if (!entryPath) return null;
  let stat;
  try {
    stat = fs.lstatSync(entryPath);
    if (!stat.isFile()) return null;
  } catch { return null; }
  const normalizedMachine = publicMachineLabel(manifest.machine || machine);
  const key = artifactKey(manifest.repo, manifest.id, normalizedMachine);
  if (!key) return null;
  return {
    artifactKey: key,
    id: String(manifest.id),
    repo: String(manifest.repo),
    machine: normalizedMachine,
    title: String(manifest.title),
    summary: String(manifest.summary || ''),
    tags: Array.isArray(manifest.tags) ? manifest.tags.map(String) : [],
    screen: String(manifest.screen || ''),
    updatedAt: stat.mtime.toISOString(),
    entryUrl: pathToFileURL(entryPath).href,
    fileName: path.basename(entryPath),
    sizeBytes: stat.size,
    source: { localFallback: true },
  };
}

function legacyHtmlArtifact(filePath, machine) {
  let stat;
  try {
    stat = fs.statSync(filePath);
    if (!stat.isFile()) return null;
  } catch { return null; }
  const repo = repoNameForArtifact(filePath);
  const id = path.basename(filePath).replace(/\.html?$/i, '');
  const normalizedMachine = publicMachineLabel(machine);
  const key = artifactKey(repo, id, normalizedMachine);
  if (!key) return null;
  return {
    artifactKey: key,
    id,
    repo,
    machine: normalizedMachine,
    title: htmlTitle(filePath) || id.replace(/[-_]/g, ' '),
    summary: '',
    tags: [],
    screen: '',
    updatedAt: stat.mtime.toISOString(),
    entryUrl: pathToFileURL(filePath).href,
    fileName: path.basename(filePath),
    sizeBytes: stat.size,
    source: { legacyHtml: true, localFallback: true },
  };
}

function listLocalArtifacts(config = {}, machine = 'hosta') {
  const artifacts = [];
  const seen = new Set();
  const normalizedMachine = publicMachineLabel(machine);
  for (const dir of reviewArtifactSearchDirs(config)) {
    for (const entry of safeReadDir(dir)) {
      let artifact = null;
      if (entry.isDirectory()) {
        artifact = manifestArtifact(path.join(dir, entry.name, 'manifest.json'), normalizedMachine);
      } else if (entry.isFile() && /\.html?$/i.test(entry.name)) {
        artifact = legacyHtmlArtifact(path.join(dir, entry.name), normalizedMachine);
      }
      if (!artifact || seen.has(artifact.artifactKey)) continue;
      seen.add(artifact.artifactKey);
      artifacts.push(artifact);
    }
  }
  artifacts.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
  return {
    schema: 'pentacle.uiReviewIndex.v1',
    generatedAt: new Date().toISOString(),
    machine: normalizedMachine,
    source: 'local-fixture',
    artifacts,
  };
}

function normalizeHubIndex(envelope, connected) {
  if (!envelope) return null;
  const data = envelope.data && typeof envelope.data === 'object' ? envelope.data : {};
  const received = envelope.server_received_at ? Date.parse(envelope.server_received_at) : NaN;
  const ageSec = Number.isFinite(received) ? Math.max(0, Date.now() - received) / 1000 : null;
  const ttl = Number.isFinite(Number(envelope.freshness_ttl_sec)) ? Number(envelope.freshness_ttl_sec) : 300;
  const base = {
    schema: data.schema || 'pentacle.uiReviewIndex.v1',
    generatedAt: data.generatedAt || envelope.updated_at || new Date().toISOString(),
    source: 'adapter',
    artifacts: [],
    _updated_at: envelope.updated_at,
    _server_received_at: envelope.server_received_at,
    _age_sec: ageSec,
    _transport_stale: !connected,
    _data_stale: ageSec != null ? ageSec > ttl : false,
  };
  if (data.schema && data.schema !== 'pentacle.uiReviewIndex.v1') {
    return { ...base, schema: data.schema, error: 'Malformed UI review index: unsupported schema.' };
  }
  const invalidArtifacts = [];
  const artifacts = Array.isArray(data.artifacts) ? data.artifacts.map((item) => {
    const normalized = {
      ...item,
      artifactKey: item && (item.artifactKey || artifactKey(item.repo, item.id, publicMachineLabel(item.machine))),
      entryUrl: item && (item.entryUrl || item.url || ''),
      updatedAt: item && item.updatedAt || '',
      machine: publicMachineLabel(item && item.machine),
      tags: item && Array.isArray(item.tags) ? item.tags : [],
      source: {},
    };
    if (!normalized.artifactKey || !normalized.entryUrl || !normalized.repo || !normalized.title || !normalized.updatedAt) {
      invalidArtifacts.push(item);
      return null;
    }
    return normalized;
  }).filter(Boolean) : [];
  artifacts.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
  return {
    ...base,
    ...data,
    artifacts,
    invalidArtifactCount: invalidArtifacts.length,
    warning: invalidArtifacts.length ? `${invalidArtifacts.length} invalid artifact(s) were skipped.` : data.warning,
  };
}

module.exports = {
  UI_REVIEW_DASHBOARD_ID,
  artifactKey,
  expandHome,
  listLocalArtifacts,
  normalizeHubIndex,
  safeBundleEntry,
  slug,
};
