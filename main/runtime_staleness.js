const { execFile } = require('child_process');

const GIT_TIMEOUT_MS = 5000;
const STALENESS_CACHE_TTL_MS = 60_000;
const SHA_RE = /^[0-9a-f]{40}$/i;

function runGit(args) {
  return new Promise((resolve, reject) => {
    execFile('git', args, {
      encoding: 'utf8',
      timeout: GIT_TIMEOUT_MS,
      maxBuffer: 64 * 1024,
      windowsHide: true,
      env: {
        ...process.env,
        GIT_TERMINAL_PROMPT: '0',
        GIT_SSH_COMMAND: process.env.GIT_SSH_COMMAND || 'ssh -o BatchMode=yes -o ConnectTimeout=3',
      },
    }, (error, stdout, stderr) => {
      if (error) {
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve(stdout);
    });
  });
}

function unknownRuntimeStaleness(result, reason) {
  return { ...result, state: 'unknown', reason };
}

function originMainSha(raw) {
  const line = String(raw || '').split(/\r?\n/).find((value) => /\srefs\/heads\/main$/.test(value));
  const sha = line && line.trim().split(/\s+/)[0];
  return SHA_RE.test(sha || '') ? sha : null;
}

async function collectRuntimeStaleness({ repoPath, now = () => new Date().toISOString(), run = runGit } = {}) {
  const result = {
    sha: null,
    originMainSha: null,
    commitsBehind: null,
    state: 'unknown',
    reason: null,
    stale: false,
    checkedAt: now(),
  };
  if (!repoPath) return unknownRuntimeStaleness(result, 'repo_unavailable');

  try {
    result.sha = String(await run(['-C', repoPath, 'rev-parse', '--verify', 'HEAD'])).trim();
  } catch {
    return unknownRuntimeStaleness(result, 'head_unavailable');
  }
  if (!SHA_RE.test(result.sha)) return unknownRuntimeStaleness(result, 'head_unavailable');

  try {
    await run(['-C', repoPath, 'symbolic-ref', '-q', 'HEAD']);
  } catch {
    return unknownRuntimeStaleness(result, 'detached_head');
  }

  let upstream;
  try {
    upstream = String(await run(['-C', repoPath, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'])).trim();
  } catch {
    return unknownRuntimeStaleness(result, 'upstream_unavailable');
  }
  if (upstream !== 'origin/main') return unknownRuntimeStaleness(result, 'upstream_not_origin_main');

  try {
    result.originMainSha = originMainSha(await run(['-C', repoPath, 'ls-remote', '--exit-code', 'origin', 'refs/heads/main']));
  } catch {
    return unknownRuntimeStaleness(result, 'origin_unavailable');
  }
  if (!result.originMainSha) return unknownRuntimeStaleness(result, 'origin_main_unavailable');

  try {
    await run(['-C', repoPath, 'cat-file', '-e', `${result.originMainSha}^{commit}`]);
  } catch {
    try {
      // Fetch into the object database only when the remote tip is absent. The
      // Clearing configured refmaps plus --no-write-fetch-head leaves the worktree,
      // checked-out ref, remote-tracking refs, and FETCH_HEAD untouched.
      await run(['-C', repoPath, 'fetch', '--no-write-fetch-head', '--no-tags', '--refmap=', 'origin', 'refs/heads/main']);
      await run(['-C', repoPath, 'cat-file', '-e', `${result.originMainSha}^{commit}`]);
    } catch {
      return unknownRuntimeStaleness(result, 'origin_history_unavailable');
    }
  }

  try {
    const count = String(await run(['-C', repoPath, 'rev-list', '--count', `${result.sha}..${result.originMainSha}`])).trim();
    if (!/^\d+$/.test(count)) return unknownRuntimeStaleness(result, 'origin_history_unavailable');
    result.commitsBehind = Number(count);
  } catch {
    return unknownRuntimeStaleness(result, 'origin_history_unavailable');
  }

  return {
    ...result,
    state: result.commitsBehind === 0 ? 'current' : 'behind',
    reason: null,
  };
}

function createRuntimeStalenessCache({
  collect = collectRuntimeStaleness,
  now = () => Date.now(),
  ttlMs = STALENESS_CACHE_TTL_MS,
} = {}) {
  let value = {
    sha: null,
    originMainSha: null,
    commitsBehind: null,
    state: 'unknown',
    reason: 'refresh_pending',
    stale: false,
    checkedAt: null,
  };
  let refreshedAt = 0;
  let hasRefreshResult = false;
  let refreshPromise = null;

  function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = Promise.resolve()
      .then(collect)
      .then((next) => {
        value = { ...next, stale: false };
        refreshedAt = now();
        hasRefreshResult = true;
      })
      .catch(() => {
        value = {
          sha: null,
          originMainSha: null,
          commitsBehind: null,
          state: 'unknown',
          reason: 'refresh_failed',
          stale: false,
          checkedAt: new Date().toISOString(),
        };
        refreshedAt = now();
        hasRefreshResult = true;
      })
      .finally(() => {
        refreshPromise = null;
      });
    return refreshPromise;
  }

  return {
    get() {
      if (now() - refreshedAt >= ttlMs) {
        void refresh();
        return hasRefreshResult ? { ...value, stale: true } : { ...value };
      }
      return { ...value };
    },
    refresh,
  };
}

module.exports = {
  GIT_TIMEOUT_MS,
  STALENESS_CACHE_TTL_MS,
  collectRuntimeStaleness,
  createRuntimeStalenessCache,
};
