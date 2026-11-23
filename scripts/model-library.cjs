#!/usr/bin/env node
'use strict';

const { checkDrop, createLibrary, DEFAULT_LIMITS } = require('../lib/model_library.cjs');

function run(argv, env = process.env) {
  const [command, ...flags] = argv;
  const allowed = { check:['drop','root','max-bytes','max-triangles'], publish:['drop','root','max-bytes','max-triangles','rename-project'],
    verify:['root'], remove:['root','project','model'], rollback:['root','project','model','revision'],
    'link-preview':['root','project','model','view'] };
  if (!Object.hasOwn(allowed,command)) throw new Error('Use check, publish, verify, remove, rollback or link-preview');
  const args = Object.create(null);
  for (let i=0;i<flags.length;i++) {
    const key = flags[i].slice(2);
    if (!flags[i].startsWith('--') || !allowed[command].includes(key) || Object.hasOwn(args,key)) throw new Error('Unknown or repeated option');
    if (key === 'rename-project') args[key] = true;
    else if (!flags[i+1] || flags[i+1].startsWith('--')) throw new Error('Supply a value for --' + key);
    else args[key] = flags[++i];
  }
  const limits = {};
  for (const [k,n] of Object.entries(DEFAULT_LIMITS)) {
    const v = args['max-'+k];
    if (v !== undefined && !/^\d+$/.test(v)) throw new Error('Use a positive integer for --max-' + k);
    limits[k] = v === undefined ? n : Number(v);
  }
  if (['check','publish'].includes(command) && !args.drop) throw new Error('Supply --drop <directory>');
  if (command === 'check') {
    const {ok,diagnostics,measurements} = checkDrop(args.drop,{limits});
    return {ok,diagnostics,measurements};
  }
  const library = createLibrary(args.root ?? env.PENTACLE_MODEL_LIBRARY_ROOT);
  if (command === 'publish') {
    return library.publish(args.drop,{limits,rename:args['rename-project']}).then(({ok,diagnostics,measurements,revision,published_at,unchanged}) =>
      ({ok,diagnostics,measurements,revision,published_at,unchanged}));
  }
  if (command === 'verify') return library.verify();
  if (command === 'remove') return library.remove(args.project,args.model);
  if (command === 'rollback') return library.rollback(args.project,args.model,args.revision);
  return library.preview(args.project,args.model,args.view);
}

if (require.main === module) Promise.resolve().then(()=>run(process.argv.slice(2))).then(result=>{
  console.log(JSON.stringify(result,null,2));
  if (result.ok === false) process.exitCode=1;
}).catch(error=>{ console.error(error.message); process.exitCode=1; });
module.exports = { run };
