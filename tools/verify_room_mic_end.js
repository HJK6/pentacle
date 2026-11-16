'use strict';
const fs=require('node:fs');
const {createRoomMicTurns}=require('../renderer/room_mic_turns');
async function main(){
  const [endpoint,deliveryPath,eventsPath,outputPath]=process.argv.slice(2);
  const delivery=JSON.parse(fs.readFileSync(deliveryPath,'utf8'));
  const events=JSON.parse(fs.readFileSync(eventsPath,'utf8')).events;
  const calls=[];
  const outcomes=[];
  const tracker=createRoomMicTurns({api:async(method,path,body)=>{
    calls.push({method,path,body});
    const response=await fetch(endpoint+path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    return response.json();
  },onOutcome:result=>outcomes.push(result)});
  tracker.register(delivery.turn);
  for(const event of events)tracker.observe({type:'chat.event',event});
  for(const event of events)tracker.observe({type:'chat.event',event});
  const deadline=Date.now()+5000;
  while(outcomes.length<calls.length&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,20));
  if(calls.length!==1||outcomes.length!==1)throw Error('Exactly one turn-ended call did not complete');
  fs.writeFileSync(outputPath,JSON.stringify({calls,outcomes},null,2)+'\n');
  console.log(JSON.stringify({turn_ended_calls:calls.length,outcome:outcomes[0].result}));
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
