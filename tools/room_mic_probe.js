'use strict';
// A capture counterpart plus the real wake-delivery subject. The caller supplies
// a disposable seat explicitly; this harness never reads or writes Bart binding.
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const {createWakeDelivery}=require('../renderer/wake_delivery');

async function main() {
  const [endpoint,seat,msgId,inputPath,outputPath]=process.argv.slice(2);
  const url=new URL(endpoint);
  if(url.protocol!=='http:'||!['127.0.0.1','[::1]'].includes(url.hostname))throw Error('A loopback fixture is required');
  if(!seat||seat==='bart:assistant'||!msgId||!inputPath||!outputPath)throw Error('Explicit disposable fixture arguments required');
  const input=fs.readFileSync(inputPath,'utf8');
  const api=async(method,path,body)=>{
    const result=await fetch(endpoint+path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    return result.json();
  };
  await api('POST','/fixture/capture',{id:msgId,text:input});
  let delivered;
  const delivery=createWakeDelivery({
    config:{features:{mic:true},chatStream:{}},
    getState:async()=>({connected:true,sessions:[{stream_id:seat,session_generation:'fixture-seat',status:'open',pane_status:'pane_alive'}]}),
    getBinding:async()=>({ok:true,source:'durable',stream_id:seat,generation:'fixture-seat'}),api,
    sendTurn:(stream,text)=>{
      const receipt=JSON.parse(execFileSync('agent-orch',['send',stream,msgId,text],{encoding:'utf8',timeout:60000}));
      if(receipt.ok===false && !receipt.queued_for_redelivery)throw Error('Disposable delivery failed');
      delivered={stream,text,receipt};
      return 'fixture-optimistic-'+msgId;
    },
    onRoomMicTurn:turn=>{delivered={...delivered,turn};},
  });
  await delivery.tick(await api('GET','/status'));
  if(!delivered)throw Error('Fixture was not delivered');
  fs.writeFileSync(outputPath,JSON.stringify(delivered,null,2)+'\n');
  console.log(JSON.stringify({seat,conversation_id:delivered.turn.conversationId,delivery:delivered.receipt.delivery}));
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
