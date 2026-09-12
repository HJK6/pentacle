#!/usr/bin/env node
'use strict';
// Full shipped renderer in Electron and Chrome; isolated profile/daemon/tmux
// fixtures. Reuses the public CDP and web-host harness, never the live app.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {spawn,execFileSync}=require('node:child_process');
const cdp=require('./lib/cdp');
const {startDaemon,writeProfile,freePort,onceExit}=require('./web_gate');
const {main:startHost}=require('../../server');
const {geometry,settle,runGridSplit,runGridSplitTerminals,runGridSplitWidths}=require('./lib/grid_split_scenario');
const ROOT=path.resolve(__dirname,'../..');
async function run(surface,output) {
  fs.mkdirSync(output,{recursive:true});
  const scratch=fs.mkdtempSync(path.join(os.tmpdir(),'pentacle-split-'));
  const runtime={},steps=[],cleanup=[];
  const report={ok(name,ok,detail){steps.push({name,ok,detail});if(!ok)throw Object.assign(Error(name),{classification:'PRODUCT_FAIL'});}};
  let host,proc,session,error;
  const fixtures=Array.from({length:4},(_,i)=>({host:'local',sessionName:`ptest-split-${process.pid}-${i}`}));
  const owned=[];
  const tmux=args=>execFileSync('tmux',args,{encoding:'utf8',stdio:['ignore','pipe','pipe']}).trim();
  const files={};for(const file of ['renderer/app.js','renderer/index.html','renderer/styles.css','renderer/grid_col_resizer.js','renderer/dist/web/bundle.js','test/e2e/grid_split_gate.js','test/e2e/lib/grid_split_scenario.js','test/e2e/web_gate.js'])
    if(fs.existsSync(path.join(ROOT,file)))files[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT,file))).digest('hex');
  const log=fs.openSync(path.join(output,'runtime.log'),'w');
  try {
    for(const fixture of fixtures){owned.push(fixture.sessionName);tmux(['new-session','-d','-s',fixture.sessionName,'-x','80','-y','24','sh','-c','stty raw -echo; exec cat']);}
    const daemon=await startDaemon({python:process.env.PENTACLE_PYTHON||'python3',timeoutMs:30000},scratch,runtime,fixtures);
    const profile=writeProfile(scratch,daemon.port),port=await freePort();
    const profileSource=fs.readFileSync(profile,'utf8').replace('"mic": false','"mic": false, "chatHarnessTelemetry": true, "chatUi": true');
    fs.writeFileSync(profile,profileSource);
    const args=[`--remote-debugging-port=${port}`,`--user-data-dir=${path.join(scratch,'profile')}`];
    if(surface==='web') {
      host=await startHost(['--profile',profile,'--port','0']);
      const chrome=process.env.PENTACLE_CHROME || (process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'google-chrome');
      proc=spawn(chrome,['--headless=new','--no-first-run','--no-default-browser-check','--disable-gpu','--window-size=1280,800',...args,`http://127.0.0.1:${host.port}`],{stdio:['ignore',log,log]});
    } else {
      proc=spawn(require('electron'),[ROOT,...args],{env:{...process.env,PENTACLE_CONFIG:profile,PENTACLE_HARNESS:'1'},stdio:['ignore',log,log]});
    }
    session=await cdp.connect(port);
    await session.send('Page.bringToFront');
    await session.eval(`window.__splitPointerEvents=[]; for(const type of ['pointerdown','pointermove','pointerup','lostpointercapture']) document.addEventListener(type,e=>window.__splitPointerEvents.push({type,target:e.target.className,x:e.clientX,y:e.clientY,primary:e.isPrimary,button:e.button}),true);`);
    await runGridSplit({session,report,cdp});
    await runGridSplitTerminals({session,report,cdp,fixtures,tmux});
    await runGridSplitWidths({session,report});
    if(surface==='desktop') {
      const before=await session.eval(geometry),oldPid=proc.pid;
      session.close();session=null;proc.kill('SIGTERM');await onceExit(proc);
      if(proc.exitCode===null&&proc.signalCode===null)throw Error('old isolated Electron did not exit before relaunch');
      proc=spawn(require('electron'),[ROOT,...args],{env:{...process.env,PENTACLE_CONFIG:profile,PENTACLE_HARNESS:'1'},stdio:['ignore',log,log]});
      session=await cdp.connect(port);await session.send('Page.bringToFront');
      await session.waitFor("document.readyState==='complete' && !!document.querySelector('.grid-col-resizer')?.getAttribute('aria-valuenow')");
      await settle(session);
      const after=await session.eval(geometry);
      report.ok('desktop process relaunch restores the same saved split',oldPid!==proc.pid&&before.saved===after.saved&&Math.abs(before.cells[0].width-after.cells[0].width)<2,{oldPid,newPid:proc.pid,before,after});
    }
  } catch(e) {error={classification:e.classification||'HARNESS_ERROR',message:e.message,stack:e.stack};}
  finally {
    if(session) {try{fs.writeFileSync(path.join(output,'pointer.json'),JSON.stringify(await session.eval(`({events:window.__splitPointerEvents,focus:document.hasFocus(),style:document.querySelector('.grid')?.getAttribute('style'),app:typeof gridColResizer,handle:document.querySelector('.grid-col-resizer')?.outerHTML})`),null,2));await session.screenshot(path.join(output,'surface.png'));fs.writeFileSync(path.join(output,'console.json'),JSON.stringify(session.consoleLines,null,2));}catch(e){cleanup.push(e.message);}session.close();}
    if(proc){proc.kill('SIGTERM');await onceExit(proc);if(proc.exitCode===null&&proc.signalCode===null){proc.kill('SIGKILL');await onceExit(proc);}cleanup.push({appExited:proc.exitCode!==null||proc.signalCode!==null});}
    if(host)try{await host.close();}catch(e){cleanup.push({webHostClosed:false,error:e.message});}
    if(runtime.daemonProc){runtime.daemonProc.kill('SIGTERM');await onceExit(runtime.daemonProc);cleanup.push({daemonExited:runtime.daemonProc.exitCode!==null||runtime.daemonProc.signalCode!==null});}
    for(const name of owned){try{tmux(['kill-session','-t','='+name]);}catch(e){cleanup.push({session:name,error:e.message});}}
    for(const name of owned){try{tmux(['has-session','-t','='+name]);cleanup.push({session:name,closed:false});}catch(e){cleanup.push({session:name,closed:e.status===1&&/can't find session|no server running|No such file/.test(String(e.stderr))});}}
    fs.closeSync(log);fs.rmSync(scratch,{recursive:true,force:true});
  }
  if(cleanup.some(c=>typeof c==='string'||c.error||Object.values(c).includes(false)))error={classification:'CLEANUP_FAIL',prior:error,cleanup};
  const result={surface,source:execFileSync('git',['rev-parse','HEAD'],{cwd:ROOT,encoding:'utf8'}).trim(),files,steps,error,cleanup,status:error?'FAIL':'PASS'};
  fs.writeFileSync(path.join(output,'verdict.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify({surface,status:result.status,error,output}));return result;
}
if(require.main===module) {
  const surface=process.argv[2]||'web',output=path.resolve(process.argv[3]||fs.mkdtempSync(path.join(os.tmpdir(),'pentacle-split-results-')));
  run(surface,output).then(r=>{process.exitCode=r.error?1:0;}).catch(e=>{console.error(e);process.exitCode=2;});
}
module.exports={run};
