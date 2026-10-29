#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const store = require('../main/review_store');

const HELP = `review-item --dir ABSOLUTE_DIR add --kind review|answer|input --title TEXT
  [--project Agenteach|DUMBO|Pentacle|Personal|Other]
  [--dispatch-id ID] [--reply-to-message-id ID] [--link HTTPS_URL]
  [--first-raised-at RFC3339] [--origin-note TEXT] [--smoke-run 32_HEX]
review-item --dir ABSOLUTE_DIR list [--state pending|done|resolved|all] [--json]
review-item --dir ABSOLUTE_DIR resolve ID --reason TEXT
review-item --dir ABSOLUTE_DIR seed SEED.json
review-item --dir ABSOLUTE_DIR smoke-remove ID --smoke-run 32_HEX
review-item --dir ABSOLUTE_DIR validate
review-item --version
Only review artifacts, answers to Sai, and questions needing Sai belong here.
No status updates, progress notes, FYIs or acknowledgements.
"Got it" means received/reviewed only. It never approves a decision, completes
an external task, or grants permission to execute one. Resolve is Lews's item
closure, not Sai's acknowledgement, and never edits the ack file.`;
function parse(argv) {
  const positional = [], opts = {};
  const flags = new Set(['json', 'version', 'help']);
  for (let i = 0; i < argv.length; i++) {
    const word = argv[i];
    if (!word.startsWith('--')) { positional.push(word); continue; }
    const key = word.slice(2).replaceAll('-', '_');
    if (key in opts) throw new store.ReviewError('duplicate_option', 'duplicate_option', 422, 2);
    if (flags.has(key)) opts[key] = true;
    else {
      if (!['dir', 'kind', 'title', 'project', 'dispatch_id', 'reply_to_message_id', 'link', 'first_raised_at', 'origin_note', 'smoke_run', 'state', 'reason'].includes(key)
        || i + 1 >= argv.length || argv[i + 1].startsWith('--')) throw new store.ReviewError('invalid_option', 'invalid_option', 422, 2);
      opts[key] = argv[++i];
    }
  }
  return { positional, opts };
}
function only(opts, allowed) {
  for (const key of Object.keys(opts)) if (!allowed.includes(key)) throw new store.ReviewError('invalid_option', 'invalid_option', 422, 2);
}
function version() {
  try { const value = fs.readFileSync(path.join(__dirname, '..', 'VERSION'), 'utf8').trim(); return /^[0-9a-f]{40}$/.test(value) ? value : 'uninstalled'; }
  catch { return 'uninstalled'; }
}
function main(argv = process.argv.slice(2)) {
  const { positional, opts } = parse(argv);
  if (opts.help) { console.log(HELP); return 0; }
  if (opts.version) { only(opts, ['version', 'dir']); if (positional.length) throw new store.ReviewError('invalid_option', 'invalid_option', 422, 2); console.log(version()); return 0; }
  if (!opts.dir || !path.isAbsolute(opts.dir)) throw new store.ReviewError('directory_must_be_absolute', 'directory_must_be_absolute', 422, 2);
  const [command, arg] = positional;
  let result, line;
  if (command === 'add' && positional.length === 1) {
    only(opts, ['dir', 'kind', 'title', 'project', 'dispatch_id', 'reply_to_message_id', 'link', 'first_raised_at', 'origin_note', 'smoke_run', 'json']);
    if (opts.project !== undefined && (!store.validProject(opts.project) || opts.smoke_run !== undefined))
      throw new store.ReviewError('invalid_project', `project must be one of ${store.PROJECTS.join(', ')}; smoke items use Other`, 422, 2);
    result = store.addItem(opts.dir, opts);
    line = `added ${result.item.id} ${result.item.created_at}`;
  } else if (command === 'list' && positional.length === 1) {
    only(opts, ['dir', 'state', 'json']);
    const state = opts.state || 'pending';
    if (!['pending', 'done', 'resolved', 'all'].includes(state)) throw new store.ReviewError('invalid_state', 'invalid_state', 422, 2);
    const feed = store.projection(opts.dir);
    if (feed.state !== 'ok') throw new store.ReviewError(feed.error_code || `store_${feed.state}`);
    result = { state: feed.state, read_at: feed.read_at, updated_at: feed.updated_at,
      stale: feed.stale, warning: feed.warning, items: feed.items.filter(i => state === 'all' || i.state === state) };
    line = `listed ${result.items.length} ${result.read_at}${result.stale ? ' stale' : ' fresh'}${result.warning ? ' Inbox nearing capacity' : ''}\nPROJECT\tSTATE\tKIND\tTITLE\n${result.items.map(item => `${item.project}\t${item.state}\t${item.kind}\t${item.title}`).join('\n')}`;
  } else if (command === 'resolve' && positional.length === 2) {
    only(opts, ['dir', 'reason', 'json']);
    result = store.resolveItem(opts.dir, arg, opts.reason);
    line = `${result.changed ? 'resolved' : 'already resolved'} ${arg} ${result.item.resolved_at}`;
  } else if (command === 'seed' && positional.length === 2) {
    only(opts, ['dir', 'json']);
    const raw = fs.readFileSync(arg);
    result = store.seedItems(opts.dir, raw);
    line = `${result.changed ? 'seeded' : 'seed unchanged'} ${result.count} ${new Date().toISOString()}`;
  } else if (command === 'smoke-remove' && positional.length === 2) {
    only(opts, ['dir', 'smoke_run', 'json']);
    result = store.smokeRemove(opts.dir, arg, opts.smoke_run);
    line = `smoke removed ${arg} ${new Date().toISOString()}`;
  } else if (command === 'validate' && positional.length === 1) {
    only(opts, ['dir', 'json']);
    const feed = store.projection(opts.dir);
    if (feed.state !== 'ok') throw new store.ReviewError(feed.error_code || `store_${feed.state}`);
    result = { state: 'ok', count: feed.items.length, warning: feed.warning };
    line = `validated ${result.count} ${feed.read_at}${feed.warning ? ' Inbox nearing capacity' : ''}`;
  } else throw new store.ReviewError('invalid_command', 'invalid_command', 422, 2);
  if (opts.json) console.log(JSON.stringify(result)); else console.log(line);
  return 0;
}
if (require.main === module) {
  try { process.exitCode = main(); }
  catch (e) { console.error(e instanceof store.ReviewError ? e.message : 'io_failed'); process.exitCode = e instanceof store.ReviewError ? e.exit : 5; }
}
module.exports = { main, parse, HELP };
