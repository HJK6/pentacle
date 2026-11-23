'use strict';
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {drop}=require('../../fixtures/model_library.cjs');

const operator='forge-operator@example.test', general='forge-general@example.test';
const origin='https://fixture.example.test';
const tailnetArgs=(login,binding=false)=>['--auth','tailscale','--origin',origin,'--allow-login',login,
  ...(binding ? ['--operator-login',login] : [])];
const proxy=(login=operator)=>({'tailscale-user-login':login,'x-forwarded-proto':'https',
  'x-forwarded-for':'100.64.8.4',origin});

function writeFixture(root) {
  const source=path.join(root,'drop'), library=path.join(root,'models');
  const profile=path.join(root,'profile.js');
  fs.writeFileSync(profile,"module.exports={features:{mic:false},chatStream:{localHost:'fixture',hosts:['local']}};\n");
  drop(source,{edit:s=>{
    s.views[0].parts[0].color='#828182';
    s.views.push({...structuredClone(s.views[0]),id:'opposite',title:'Opposite',camera:{turn:140,tilt:20}});
  }});
  return {source,library,profile};
}
function request(port, url, {headers={},method='GET',body}={}) {
  return new Promise((resolve,reject)=>{
    const req=http.request({hostname:'127.0.0.1',port,path:url,headers,method},res=>{
      const chunks=[];
      res.on('data',x=>chunks.push(x));
      res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks)}));
      res.on('error',reject);
    });
    req.setTimeout(5000,()=>req.destroy(new Error('Fixture HTTP timeout')));
    req.on('error',reject);
    req.end(body);
  });
}
async function withHost(fixture,flags,fn) {
  const {main}=require('../../../server/index.js');
  const env={ PENTACLE_MODEL_LIBRARY_ROOT:fixture.library, PENTACLE_DESIGN_LIBRARY_ROOT:fixture.design };
  const saved=Object.fromEntries(Object.keys(env).map(k=>[k,process.env[k]]));
  for (const [k,v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k]; else process.env[k]=v;
  }
  let host;
  const listeners = Object.fromEntries(['SIGINT','SIGTERM'].map(signal=>[signal,process.listeners(signal)]));
  try {
    try { host=await main(['--port','0','--profile',fixture.profile,...flags]); }
    finally {
      for (const [signal,previous] of Object.entries(listeners)) {
        for (const listener of process.listeners(signal)) if (!previous.includes(listener)) process.removeListener(signal,listener);
      }
    }
    return await fn(host);
  } finally {
    try { if (host) await host.close(); }
    finally {
      for (const [k,v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k]; else process.env[k]=v;
      }
    }
  }
}
module.exports={operator,general,origin,proxy,tailnetArgs,writeFixture,request,withHost};
