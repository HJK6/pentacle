#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { loadConfig } = require('../config-loader');

const PRIVATE_PARENT_MASK = 0o077;
const PRIVATE_TOKEN_MODE = 0o600;

function expandHome(value, homeDir = os.homedir()) {
  const text = String(value || '');
  if (text === '~') return homeDir;
  if (text.startsWith('~/')) return path.join(homeDir, text.slice(2));
  return text;
}

function resolveOperatorTokenPath({ root = process.cwd(), config, homeDir = os.homedir(), tokenPath } = {}) {
  const configured = tokenPath === undefined ? config?.chatStream?.tokenPath : tokenPath;
  const value = String(configured || '').trim()
    || path.join(homeDir, '.config', 'pentacle-stream', 'token');
  return path.resolve(root, expandHome(value, homeDir));
}

function formatMode(stat) {
  if (!stat) return 'missing';
  return `0${(stat.mode & 0o777).toString(8).padStart(3, '0')}`;
}

function checkOperatorToken(tokenPath, { fsImpl = fs } = {}) {
  const resolvedTokenPath = path.resolve(String(tokenPath));
  const parentPath = path.dirname(resolvedTokenPath);
  const failures = [];
  let tokenStat;

  try {
    tokenStat = fsImpl.lstatSync(resolvedTokenPath);
  } catch (error) {
    if (error?.code === 'ENOENT') {
      return {
        ok: true,
        skipped: true,
        tokenPath: resolvedTokenPath,
        parentPath,
        parentMode: 'missing',
        tokenMode: 'missing',
        failures,
      };
    }
    failures.push(`path=${resolvedTokenPath} mode=unreadable (token must be a regular file with mode 0600)`);
  }

  let parentStat;
  try {
    parentStat = fsImpl.lstatSync(parentPath);
  } catch (error) {
    failures.push(`path=${parentPath} mode=${error?.code === 'ENOENT' ? 'missing' : 'unreadable'} (token parent must be an owner-private directory)`);
  }
  if (parentStat && (!parentStat.isDirectory() || (parentStat.mode & PRIVATE_PARENT_MASK))) {
    failures.push(`path=${parentPath} mode=${formatMode(parentStat)} (token parent must be owner-private)`);
  }

  if (tokenStat && (!tokenStat.isFile() || tokenStat.isSymbolicLink() || (tokenStat.mode & 0o777) !== PRIVATE_TOKEN_MODE)) {
    failures.push(`path=${resolvedTokenPath} mode=${formatMode(tokenStat)} (token must be a regular file with mode 0600)`);
  }

  return {
    ok: failures.length === 0,
    tokenPath: resolvedTokenPath,
    parentPath,
    parentMode: formatMode(parentStat),
    tokenMode: formatMode(tokenStat),
    failures,
  };
}

function parseArgs(argv) {
  const args = { root: process.cwd(), tokenPath: undefined };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') {
      args.help = true;
    } else if (arg === '--root' || arg === '--token-path') {
      const value = argv[++index];
      if (!value) throw new Error(`${arg} requires a value`);
      args[arg === '--root' ? 'root' : 'tokenPath'] = value;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.help) {
    console.log('Usage: preflight_operator_token.js [--root REPO_ROOT] [--token-path TOKEN_PATH]');
    return 0;
  }

  let config;
  if (args.tokenPath === undefined) {
    config = loadConfig(args.root).config;
    if (String(config?.chatStream?.token || '').trim()) {
      console.log('[deploy] Operator token preflight skipped: client uses configured inline token');
      return 0;
    }
  }
  const tokenPath = resolveOperatorTokenPath({ root: args.root, config, tokenPath: args.tokenPath });
  const result = checkOperatorToken(tokenPath);
  if (!result.ok) {
    for (const failure of result.failures) console.error(`[deploy] Operator token preflight failed: ${failure}`);
    return 1;
  }
  if (result.skipped) {
    console.log(`[deploy] Operator token preflight skipped: path=${result.tokenPath} mode=missing (client has no token file)`);
    return 0;
  }
  console.log(`[deploy] Operator token preflight passed: path=${result.tokenPath} mode=${result.tokenMode} parent_path=${result.parentPath} parent_mode=${result.parentMode}`);
  return 0;
}

if (require.main === module) {
  try {
    process.exitCode = main();
  } catch (error) {
    console.error(`[deploy] Operator token preflight failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { checkOperatorToken, formatMode, resolveOperatorTokenPath };
