'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const UI_REVIEW_DASHBOARD_ID = 'ui.review.index';

function expandHome(p) {
  if (!p || typeof p !== 'string') return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function slug(value) {
  const out = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return out || null;
}

function artifactKey(repo, id, machine) {
  const parts = [slug(repo), slug(id), slug(machine)];
  if (parts.some((part) => !part)) return null;
  return parts.join('/');
}

function safeReadDir(dir) {
  try {
    return fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function htmlTitle(filePath) {
  try {
    const sample = fs.readFileSync(filePath, 'utf8').slice(0, 12000);
    const match = sample.match(/<title[^>]*>([^<]+)<\/title>/i) || sample.match(/<h1[^>]*>([^<]+)<\/h1>/i);
    return match ? match[1].trim().replace(/\s+/g, ' ') : '';
  } catch {
    return '';
  }
}

function repoNameForArtifact(filePath) {
  const parts = filePath.split(path.sep);
  const reposIndex = parts.lastIndexOf('repos');
  if (reposIndex >= 0 && parts[reposIndex + 1]) return parts[reposIndex + 1];
  const workspaceIndex = parts.lastIndexOf('agent-workspace');
  if (workspaceIndex >= 0 && parts[workspaceIndex + 1]) return parts[workspaceIndex + 1];
  return path.basename(path.dirname(filePath));
}

function reviewArtifactSearchDirs(config = {}) {
  const configured = Array.isArray(config.artifactDirs) ? config.artifactDirs.map(expandHome) : [];
  const roots = Array.isArray(config.repoRoots) ? config.repoRoots.map(expandHome) : [path.join(os.homedir(), 'repos')];
  const dirs = new Set([
    path.join(os.homedir(), 'agent-workspace', 'ui-review'),
    ...configured,
  ]);
  for (const root of roots) {
    for (const entry of safeReadDir(root)) {
      if (!entry.isDirectory()) continue;
      const repoDir = path.join(root, entry.name);
      dirs.add(path.join(repoDir, 'test', 'artifacts'));
      dirs.add(path.join(repoDir, '.ui-review'));
    }
  }
  return Array.from(dirs);
}

function safeBundleEntry(root, entry) {
  if (!entry || typeof entry !== 'string') return null;
  if (path.isAbsolute(entry)) return null;
  if (entry.split(/[\\/]+/).includes('..')) return null;
  const resolved = path.resolve(root, entry);
  const relative = path.relative(root, resolved);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return resolved;
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
  } catch {
    return null;
  }
  const publisherMachine = manifest.machine || machine;
  const key = artifactKey(manifest.repo, manifest.id, publisherMachine);
  if (!key) return null;
  return {
    artifactKey: key,
    id: manifest.id,
    repo: manifest.repo,
    machine: publisherMachine,
    title: manifest.title,
    summary: manifest.summary || '',
    tags: Array.isArray(manifest.tags) ? manifest.tags : [],
    screen: manifest.screen || '',
    updatedAt: stat.mtime.toISOString(),
    entryUrl: pathToFileURL(entryPath).href,
    fileName: path.basename(entryPath),
    path: entryPath,
    sizeBytes: stat.size,
    source: {
      ...(manifest.source || {}),
      manifest,
      localPath: root,
      localFallback: true,
    },
  };
}

function legacyHtmlArtifact(filePath, machine) {
  let stat;
  try {
    stat = fs.statSync(filePath);
    if (!stat.isFile()) return null;
  } catch {
    return null;
  }
  const repo = repoNameForArtifact(filePath);
  const id = path.basename(filePath).replace(/\.html?$/i, '');
  const key = artifactKey(repo, id, machine);
  if (!key) return null;
  return {
    artifactKey: key,
    id,
    repo,
    machine,
    title: htmlTitle(filePath) || id.replace(/[-_]/g, ' '),
    summary: '',
    tags: [],
    screen: '',
    updatedAt: stat.mtime.toISOString(),
    entryUrl: pathToFileURL(filePath).href,
    fileName: path.basename(filePath),
    path: filePath,
    sizeBytes: stat.size,
    source: {
      legacyHtml: true,
      localFallback: true,
    },
  };
}

function listLocalArtifacts(config = {}, machine = os.hostname()) {
  const artifacts = [];
  const seen = new Set();
  for (const dir of reviewArtifactSearchDirs(config)) {
    for (const entry of safeReadDir(dir)) {
      let artifact = null;
      if (entry.isDirectory()) {
        const manifestPath = path.join(dir, entry.name, 'manifest.json');
        artifact = manifestArtifact(manifestPath, machine);
      } else if (entry.isFile() && /\.html?$/i.test(entry.name)) {
        artifact = legacyHtmlArtifact(path.join(dir, entry.name), machine);
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
    machine,
    source: 'local-fallback',
    artifacts,
  };
}

function normalizeHubIndex(env, connected) {
  if (!env) return null;
  const ageSec = env.server_received_at ? (Date.now() - new Date(env.server_received_at).getTime()) / 1000 : null;
  const ttl = env.freshness_ttl_sec != null ? env.freshness_ttl_sec : 300;
  const data = env.data || {};
  const base = {
    schema: data.schema || 'pentacle.uiReviewIndex.v1',
    generatedAt: data.generatedAt || env.updated_at || new Date().toISOString(),
    source: 'hub',
    artifacts: [],
    _updated_at: env.updated_at,
    _server_received_at: env.server_received_at,
    _age_sec: ageSec,
    _transport_stale: !connected,
    _data_stale: ageSec != null ? ageSec > ttl : false,
  };
  if (data.schema !== 'pentacle.uiReviewIndex.v1') {
    return {
      ...base,
      schema: data.schema || '',
      error: `Malformed UI Review index: expected schema pentacle.uiReviewIndex.v1, got ${data.schema || 'missing'}.`,
    };
  }
  const invalidArtifacts = [];
  const artifacts = Array.isArray(data.artifacts) ? data.artifacts.map((item) => {
    const normalized = {
      ...item,
      artifactKey: item.artifactKey || artifactKey(item.repo, item.id, item.machine),
      entryUrl: item.entryUrl || item.url || '',
      updatedAt: item.updatedAt || '',
      tags: Array.isArray(item.tags) ? item.tags : [],
      source: item.source || {},
    };
    if (!normalized.artifactKey || !normalized.entryUrl || !normalized.repo || !normalized.machine || !normalized.title || !normalized.updatedAt) {
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
    warning: invalidArtifacts.length ? `${invalidArtifacts.length} invalid UI Review artifact(s) were skipped.` : data.warning,
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
