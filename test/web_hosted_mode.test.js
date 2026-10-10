'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const os=require('node:os');const path=require('node:path');
const {main,createAuth}=require('../server/index');
for(const mode of ['identity','token','unknown']) test('effective '+mode+' overrides profile and exposes identical injected/API mode',async t=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'hosted-mode-'));const profile=path.join(dir,'profile.js');
 fs.writeFileSync(profile,"module.exports={agents:{},hosts:{local:{kind:'local'}},hostedDashboardAuthMode:'identity',dashboards:{hostedDashboardAuthMode:'identity'}}");
 const args=['--profile',profile,'--port','0'];const headers={};
 if(mode==='identity'){args.push('--auth','tailscale','--allow-login','tester@example.test','--origin','https://pentacle.example.ts.net');Object.assign(headers,{'tailscale-user-login':'tester@example.test','x-forwarded-proto':'https','x-forwarded-for':'100.64.0.10'});}
 if(mode==='token'){const file=path.join(dir,'token');fs.writeFileSync(file,'synthetic-mode-token');args.push('--token-file',file);headers.cookie=createAuth('synthetic-mode-token').setCookieHeader().split(';')[0];}
 const host=await main(args);t.after(async()=>{await host.close();fs.rmSync(dir,{recursive:true,force:true});});
 const api=await fetch(`http://127.0.0.1:${host.port}/api/config`,{headers}).then(r=>r.json());assert.equal(api.hostedDashboardAuthMode,mode);
 const page=await fetch(`http://127.0.0.1:${host.port}/`,{headers}).then(r=>r.text());
 const injected=JSON.parse(page.match(/window\.__PENTACLE_CONFIG__=(.*?);<\/script>/)[1]);assert.equal(injected.hostedDashboardAuthMode,mode);
});
