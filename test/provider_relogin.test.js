'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { registerProviderRelogin, authorizationUrl } = require('../main/provider_relogin');
const { executionHost, executionCommand, quote } = require('../main/execution_host');
const URLS = {
  codex: 'https://auth.openai.com/oauth/authorize?client_id=fixture&state=SENTINEL_STATE&code_challenge=abc&scope=openid%20profile%20offline_access',
  claude: 'https://claude.ai/oauth/authorize?client_id=fixture&state=SENTINEL_STATE&code_challenge=abc&scope=user%3Aprofile',
};
const OK = { codex: 'Logged in using ChatGPT', claude: '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","email":"private-fixture"}' };
const SUCCESS = { codex: 'Successfully logged in', claude: 'Login successful' };
function sender(id='client') {
  const s = new EventEmitter(); s.id=id; s.events=[]; s.dead=false;
  s.send=(ch,value)=>s.events.push({ch,value}); s.isDestroyed=()=>s.dead;
  s.destroy=()=>{s.dead=true;s.emit('destroyed');}; return s;
}
function harness(t, options={}) {
  const table={}, processes=[], logs=[];
  const pty={spawn(file,args,opts) {
    if(options.spawnError) throw new Error('SENTINEL_TOKEN');
    const nonce=args.join(' ').match(/__PENTACLE_RELOGIN_([a-f0-9]+):/)[1];
    let data=()=>{},exit=()=>{};
    const p={file,args,opts,writes:[],kills:0,nonce,onData:f=>{data=f;},onExit:f=>{exit=f;},
      data:d=>data(d),finish(text='',code=0,receipt=true,signal=0){data(text+`\n${receipt?'__PENTACLE_RELOGIN_'+nonce+':'+code+'\n':''}`);exit({exitCode:code,signal});},
      write(d){this.writes.push(d);if(d==='\x03' && !options.ignoreCancel)this.finish('',130);},kill(){this.kills++;exit({exitCode:0,signal:1});}};
    processes.push(p);return p;
  }};
  const config={chatStream:{localHost:'host-one',hosts:['local','peer']},peers:[{id:'peer',host:'example.invalid',user:'fixture',port:2222}]};
  const stop=registerProviderRelogin({handle:(ch,fn)=>{table[ch]=fn;}},options.config||config,{pty,log:v=>logs.push(v),cleanupMs:10,...options});
  const s=sender();
  const call=(name,...args)=>table['provider-relogin:'+name]({sender:s},...args);
  const begin=(provider='codex',host='local',id='fixture_attempt_123')=>call('start',{provider,host,id,available:true});
  const states=()=>s.events.map(e=>e.value.state);
  t.after(stop);
  return {table,processes,logs,s,call,begin,states,stop};
}
for(const provider of ['codex','claude']) {
  test(`${provider}: complete query survives chunks; verified success needs both processes`,t=>{
    const h=harness(t);assert.equal(h.begin(provider).ok,true);
    const login=h.processes[0];
    const text='\x1b[32m'+URLS[provider]+'\x1b[0m\r\n';
    for(const piece of [text.slice(0,30),text.slice(30,-1)]) login.data(piece);
    assert.equal(h.states().includes('awaiting_browser'),false);
    login.data(text.slice(-1));
    assert.equal(h.s.events.at(-1).value.url,URLS[provider]);
    login.finish(SUCCESS[provider]);
    assert.equal(h.states().at(-1),'verifying');assert.equal(h.processes.length,2);
    h.processes[1].finish(OK[provider]);
    assert.equal(h.states().at(-1),'succeeded');
    assert.equal(JSON.stringify(h.logs).includes('SENTINEL'),false);
    assert.equal(JSON.stringify(h.s.events).includes('private-fixture'),false);
    assert.equal(h.s.events.at(-1).value.url,undefined);
    assert.match(login.args.join(' '),/RUST_LOG=off/);
    assert.match(h.processes[1].args.join(' '),/RUST_LOG=off/);
    if(provider==='claude') assert.match(login.args.join(' '),/--debug-file.*\/dev\/null/);
  });
  test(`${provider}: provider-side prior-login clearing then cancel has no restoration`,async t=>{
    const h=harness(t);h.begin(provider);
    const before=h.processes[0];before.data(URLS[provider]+'\n');
    let priorAuth=null; // counterpart cleared credentials on Start
    const result=await h.call('cancel','fixture_attempt_123');
    assert.equal(result.state,'cancelled');assert.equal(priorAuth,null);
    assert.equal(h.processes.length,1);assert.doesNotMatch(before.args.join(' '),/logout|restore/);
    assert.deepEqual(before.writes,['\x03']);assert.equal(h.begin(provider).ok,true);
  });
  test(`${provider}: login failure never runs verification or restores old login`,t=>{
    const h=harness(t);h.begin(provider);h.processes[0].finish('SENTINEL_TOKEN',1);
    assert.equal(h.states().at(-1),'failed');assert.equal(h.processes.length,1);
    assert.equal(JSON.stringify(h.logs).includes('SENTINEL'),false);
  });
  test(`${provider}: wrong verification cannot report success`,t=>{
    const h=harness(t);h.begin(provider);h.processes[0].finish(SUCCESS[provider]);
    h.processes[1].finish(provider==='codex'?'Logged in using an API key - SENTINEL_TOKEN':'{"loggedIn":true,"authMethod":"oauth_token","apiProvider":"firstParty"}');
    assert.equal(h.states().at(-1),'failed');assert.equal(h.s.events.at(-1).value.reason,'verification_failed');
  });
}
test('URL allowlist refuses spoofed domains, non-auth URLs and incomplete queries',()=>{
  for(const url of ['https://auth.openai.com.evil.invalid/oauth/authorize?state=x',URLS.codex.replace('https:','http:'),URLS.codex.replace('/oauth/authorize','/login'),URLS.codex.replace('code_challenge=abc','none=x')])
    assert.equal(authorizationUrl(url+'\n','codex'),null);
  assert.equal(authorizationUrl(URLS.codex,'codex'),null);
});
for (const provider of ['claude', 'codex']) {
  for (const terminator of [String.fromCharCode(7), String.fromCharCode(27, 92)]) {
    test(`${provider}: ConPTY title before URL is discarded across chunks (${terminator.length})`, t => {
      const h = harness(t); h.begin(provider);
      const login = h.processes[0];
      const esc = String.fromCharCode(27), slash = String.fromCharCode(92);
      const title = esc + ']0;C:' + slash + 'Windows' + slash + 'ssh.exe';
      login.data('Starting sign-in\r\n' + title);
      assert.equal(h.states().includes('awaiting_browser'), false);
      login.data(terminator.slice(0, 1));
      login.data(terminator.slice(1) + esc + '[?25h' + URLS[provider].slice(0, 35));
      assert.equal(h.states().includes('awaiting_browser'), false);
      login.data(URLS[provider].slice(35) + '\r\n');
      assert.equal(h.s.events.at(-1).value.url, URLS[provider]);
      assert.equal(JSON.stringify(h.logs).includes('SENTINEL'), false);
    });
  }
  test(`${provider}: OSC payload cannot supply a sign-in URL or success marker`, t => {
    const h = harness(t); h.begin(provider);
    const esc = String.fromCharCode(27), bel = String.fromCharCode(7);
    h.processes[0].data(esc + ']0;untrusted\n' + URLS[provider] + '\n');
    assert.equal(h.states().includes('awaiting_browser'), false);
    h.processes[0].data(bel + 'ordinary output\n');
    assert.equal(h.states().includes('awaiting_browser'), false);
    h.processes[0].finish(esc + ']0;' + SUCCESS[provider] + bel);
    assert.equal(h.states().at(-1), 'failed');
    assert.equal(h.processes.length, 1);
  });
  test(`${provider}: title metadata cannot satisfy verification or target cleanup`, t => {
    const h = harness(t); h.begin(provider);
    const osc = String.fromCharCode(157), st = String.fromCharCode(156);
    h.processes[0].finish(SUCCESS[provider]);
    h.processes[1].finish(osc + '0;\n' + OK[provider] + '\n' + st);
    assert.equal(h.states().at(-1), 'failed');
    assert.equal(h.s.events.at(-1).value.reason, 'verification_failed');
    h.begin(provider);
    const login = h.processes[2];
    login.finish(osc + '0;\n__PENTACLE_RELOGIN_' + login.nonce + ':0\n' + st, 0, false);
    assert.equal(h.states().at(-1), 'cleanup_failed');
    assert.equal(h.s.events.at(-1).value.reason, 'target_exit_unconfirmed');
  });
}
test('explicit Start, provider and configured host are required; aliases share reservation',t=>{
  const h=harness(t);
  for(const req of [{},{provider:'codex',host:'local',id:'fixture_attempt_123',available:false},{provider:'__proto__',host:'local',id:'fixture_attempt_123',available:true}]) assert.equal(h.call('start',req).ok,false);
  assert.equal(h.begin('codex','missing').reason,'host_unavailable');assert.equal(h.processes.length,0);
  h.begin();
  assert.equal(h.table['provider-relogin:start']({sender:sender('other')},{provider:'claude',host:'host-one',id:'other_attempt_123',available:true}).reason,'already_running');
  assert.equal(h.processes.length,1);
});
test('code is owner-bound, Claude-only, single-use, unlogged, and cannot contain control input',t=>{
  const h=harness(t);h.begin('claude');h.processes[0].data(URLS.claude+'\n');
  assert.equal(h.table['provider-relogin:code']({sender:sender('other')},'fixture_attempt_123','SENTINEL_CODE').ok,false);
  assert.equal(h.call('code','fixture_attempt_123','abc\nexec').ok,false);
  assert.equal(h.call('code','fixture_attempt_123','SENTINEL_CODE#state').ok,true);
  assert.equal(h.call('code','fixture_attempt_123','second').ok,false);
  assert.deepEqual(h.processes[0].writes,['SENTINEL_CODE#state\r']);
  h.processes[0].data('SENTINEL_CODE#state\n');
  assert.equal(JSON.stringify(h.logs).includes('SENTINEL_CODE'),false);
  assert.equal(JSON.stringify(h.s.events).includes('SENTINEL_CODE'),false);
});
test('successful exit without login confirmation marker does not verify',t=>{
  const h=harness(t);h.begin();h.processes[0].finish('opened browser');assert.equal(h.states().at(-1),'failed');assert.equal(h.processes.length,1);
});
test('proxy exit without target receipt fails cleanup and keeps canonical host reserved',t=>{
  const h=harness(t);h.begin('codex','peer');h.processes[0].finish('',0,false);
  assert.equal(h.states().at(-1),'cleanup_failed');assert.equal(h.begin('codex','peer').reason,'already_running');
});
test('missing cancellation receipt kills only the owned proxy and does not claim clean cancel',async t=>{
  const h=harness(t,{ignoreCancel:true});h.begin();const result=await h.call('cancel','fixture_attempt_123');
  assert.equal(result.state,'cleanup_failed');assert.equal(h.processes[0].kills,1);
  assert.equal(h.begin().reason,'already_running');
});
test('timeout stops the child and distinguishes timeout from success',async t=>{
  const h=harness(t,{timeoutMs:5});h.begin();await new Promise(r=>setTimeout(r,20));
  assert.equal(h.states().at(-1),'timed_out');assert.equal(h.processes.length,1);
});
test('verification timeout, reload and shutdown cancel owned verification',async t=>{
  for(const mode of ['timeout','reload','shutdown']) {
    const h=harness(t,{verifyTimeoutMs:5});h.begin();h.processes[0].finish(SUCCESS.codex);
    if(mode==='reload')h.s.emit('did-start-navigation',{},'ignored',false,true);
    else if(mode==='shutdown')await h.stop();
    else await new Promise(r=>setTimeout(r,20));
    assert.equal(h.states().at(-1),mode==='timeout'?'timed_out':'cancelled');
    assert.deepEqual(h.processes[1].writes,['\x03']);
  }
});
test('destroyed sender and stale callbacks cannot leak output or complete a new attempt',async t=>{
  const h=harness(t);h.begin();const p=h.processes[0];h.s.destroy();const count=h.s.events.length;
  p.data(URLS.codex+'\n');p.finish(SUCCESS.codex);assert.equal(h.s.events.length,count);assert.equal(h.processes.length,1);
});
test('spawn exceptions and output overflow never reflect raw content',async t=>{
  const h=harness(t,{spawnError:true});h.begin();assert.equal(h.states().at(-1),'failed');assert.equal(JSON.stringify(h.s.events).includes('SENTINEL'),false);
  const second=harness(t);second.begin();second.processes[0].data('x'.repeat(65537));
  assert.equal(second.states().at(-1),'failed');assert.equal(second.s.events.at(-1).value.reason,'output_limit');
});
test('local, WSL and SSH commands use configured transport with preserved quoting',()=>{
  const config={chatStream:{localHost:'desktop'},localWsl:{distro:'Fixture Distro',user:'fixture'},hosts:{peer:{host:'example.invalid',user:'fixture',port:2222}}};
  const wsl=executionCommand(executionHost(config,'desktop'),['/bin/bash','-lc',"printf '%s' 'a&b'"],'win32');
  assert.equal(wsl.file,'wsl.exe');assert.deepEqual(wsl.args.slice(0,5),['-d','Fixture Distro','-u','fixture','--']);assert.match(wsl.args.at(-1),/a&b/);
  const ssh=executionCommand(executionHost(config,'peer'),['/bin/bash','-lc',"echo 'fixture'"],'win32');
  assert.equal(ssh.file,'ssh.exe');assert.deepEqual(ssh.args.slice(0,5),['-tt','-p','2222','--','fixture@example.invalid']);
  assert.equal(executionHost(config,'local').key,executionHost(config,'desktop').key);
});

test('real native PTY runs only replaced fixture executables and confirms target exit', { skip: process.platform==='win32' && 'POSIX fixture; Windows routing is separately mocked' }, async t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'relogin-fixture-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const fixture=path.join(dir,'provider-fixture');
  fs.writeFileSync(fixture,`#!/usr/bin/env node\nconst args=process.argv.slice(2); if(args.includes('status')) { console.log('Logged in using ChatGPT'); } else { console.log(${JSON.stringify(URLS.codex)}); setTimeout(()=>{ console.log('Successfully logged in'); },30); }\n`,{mode:0o700});
  const native=require('node-pty');
  let replacements=0;
  const pty={spawn(file,args,opts){
    assert.equal(file,'/bin/bash');
    const script=args.at(-1);assert.equal(script.split("'codex'").length,2,'fixture replacement must apply exactly once');
    replacements++;
    return native.spawn(file,[...args.slice(0,-1),script.replace("'codex'",quote(fixture))],{...opts,cwd:dir,env:{PATH:process.env.PATH,HOME:dir,TERM:'dumb'}});
  }};
  const table={}, s=sender();
  const stop=registerProviderRelogin({handle:(ch,fn)=>{table[ch]=fn;}},{chatStream:{localHost:'fixture'}},{pty,log:()=>{},timeoutMs:2500,cleanupMs:100});
  t.after(stop);
  const done=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('fixture did not terminate')),4000);s.send=(ch,value)=>{if(['succeeded','failed','cleanup_failed','timed_out'].includes(value.state)){clearTimeout(timer);resolve(value);}};});
  table['provider-relogin:start']({sender:s},{id:'fixture_real_attempt',provider:'codex',host:'local',available:true});
  const result=await done;assert.equal(result.state,'succeeded');assert.equal(replacements,2);
});
