'use strict';
// A capture counterpart plus the real wake-delivery subject. The caller supplies
// a disposable seat explicitly; this harness never reads or writes Bart binding.
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const {createWakeDelivery}=require('../renderer/wake_delivery');
const {createRoomMicTurns}=require('../renderer/room_mic_turns');
const client=require('../main/chat_stream_client');

async function main() {
  const [endpoint,seat,msgId,inputPath,outputPath]=process.argv.slice(2);
  const url=new URL(endpoint);
  if(url.protocol!=='http:'||!['127.0.0.1','[::1]'].includes(url.hostname))throw Error('A loopback fixture is required');
  if(!seat||!msgId||!inputPath||!outputPath)throw Error('Explicit disposable fixture arguments required');
  const inventory=JSON.parse(execFileSync('agent-orch',['inspect',seat,'--event-tail','0','--json'],{encoding:'utf8',timeout:15000}));
  const owner=process.env.PENTACLE_STREAM_ID||process.env.AGENT_ORCH_STREAM_ID;
  if(!owner||inventory.session?.parent_stream_id!==owner||inventory.session?.role!=='worker'
    ||!inventory.session?.objective?.includes('disposable'))throw Error('Target must be the caller-owned disposable fixture worker');
  const input=fs.readFileSync(inputPath,'utf8');
  const api=async(method,path,body)=>{
    const result=await fetch(endpoint+path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    return result.json();
  };
  let delivered,receiptPromise;
  const outcomes=[],frames=[];
  const tracker=createRoomMicTurns({api,onOutcome:outcome=>outcomes.push(outcome)});
  client.init({chatStream:{url:process.env.PENTACLE_PROBE_CHAT_URL||'ws://127.0.0.1:7791',
    tokenPath:process.env.PENTACLE_PROBE_TOKEN_PATH,snapshot:true}},frame=>{
    tracker.observe(frame);
    if(frame.type==='chat.event'&&frame.event?.stream_id===seat)frames.push(frame.event);
  });
  try {
  const connectedDeadline=Date.now()+15000;
  while(!client.connected&&Date.now()<connectedDeadline)await new Promise(resolve=>setTimeout(resolve,100));
  if(!client.connected)throw Error('The maintained chat client did not connect: '+client.snapshot().error);
  await api('POST','/fixture/capture',{id:msgId,text:input});
  const delivery=createWakeDelivery({
    config:{features:{mic:true},chatStream:{}},
    getState:async()=>client.snapshot(),
    getBinding:async()=>({ok:true,source:'durable',stream_id:seat,generation:inventory.session.session_generation}),api,
    sendTurn:(stream,text)=>{
      const optimisticId='fixture-optimistic-'+msgId;
      const requestId='fixture-room-send-'+msgId;
      const colon=stream.indexOf(':');
      receiptPromise=client.sendMessage({host:stream.slice(0,colon),sessionName:stream.slice(colon+1),text,optimisticId,requestId});
      delivered={stream,text,requestId};
      return optimisticId;
    },
    onRoomMicTurn:turn=>{delivered={...delivered,turn:{...turn,requestId:delivered.requestId}};tracker.register(delivered.turn);},
  });
  await delivery.tick(await api('GET','/status'));
  if(!delivered)throw Error('Fixture was not delivered');
  delivered.receipt=await receiptPromise;
  fs.writeFileSync(outputPath,JSON.stringify(delivered,null,2)+'\n');
  const finalDeadline=Date.now()+600000;
  while(!outcomes.length&&Date.now()<finalDeadline)await new Promise(resolve=>setTimeout(resolve,100));
  fs.writeFileSync(outputPath+'.frames.json',JSON.stringify({events:frames,outcomes},null,2)+'\n');
  if(outcomes.length!==1)throw Error('The actual disposable turn did not end exactly once');
  console.log(JSON.stringify({seat,conversation_id:delivered.turn.conversationId,delivery:delivered.receipt.delivery}));
  } finally {client.destroy();}
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
