'use strict';

function roomMicHeader(conversationId, text) {
  if (typeof conversationId !== 'string' || !conversationId.trim()
    || conversationId.length > 256 || /[\x00-\x1f\x7f]/.test(conversationId)) return null;
  return `[pentacle-input ${JSON.stringify({origin:'room_mic',conversation_id:conversationId})}]\n\n${text}`;
}

// Only provider transcript roots and terminal signals establish a turn. Receipt
// acknowledgement, working heartbeats and client-side quiet timers do not.
function createRoomMicTurns({ api, isSystemEndOfTurnEvent = () => false,
  onOutcome = outcome => console.info({subsystem:'room_mic',bug_ref:'voice-room-mic-202609',...outcome}) }) {
  const pending = new Map();
  const active = new Map();
  const seen = new Map();
  function register({ streamId, conversationId, optimisticId, requestId, text }) {
    if (!roomMicHeader(conversationId, '') || !streamId || !optimisticId) return;
    pending.set(conversationId, {streamId,conversationId,optimisticId,requestId,text});
  }
  function observe(frame) {
    if (['snapshot','stream_events'].includes(frame?.type) && Array.isArray(frame.events)) {
      for (const event of [...frame.events].sort((a,b)=>a.daemon_seq-b.daemon_seq)) {
        observe({type:'chat.event',event});
      }
      return;
    }
    if (frame?.type !== 'chat.event' || !frame.event) return;
    const event = frame.event;
    const streamId = event.stream_id;
    const seq = event.daemon_seq;
    if (!streamId || !Number.isFinite(seq) || seq <= (seen.get(streamId) ?? -1)) return;
    seen.set(streamId, seq);
    const raw = event.raw || {};
    if (event.client_origin || raw.is_sidechain) return;
    const kind = String(event.kind || '').toUpperCase();
    const providerRoot = ['codex-rollout','claude-jsonl'].includes(raw.transport);
    if (kind === 'USER' && providerRoot) {
      for (const [id, turn] of pending) {
        if (turn.streamId !== streamId) continue;
        if ((turn.requestId && event.request_id === turn.requestId)
          || event.optimistic_id === turn.optimisticId || event.text === turn.text) {
          pending.delete(id);
          const turns = active.get(streamId) || new Map();
          turns.set(id, turn);
          active.set(streamId, turns);
        }
      }
      return;
    }
    const final = ['ASSIST','ASSIST_TEXT'].includes(kind) && providerRoot && (
      raw.transport === 'codex-rollout' && raw.phase === 'final_answer'
      || raw.transport === 'claude-jsonl' && raw.stop_reason === 'end_turn');
    if (!final && !isSystemEndOfTurnEvent(event)) return;
    const turns = active.get(streamId);
    if (!turns) return;
    active.delete(streamId); // Remove before I/O: replay never creates another call.
    for (const id of turns.keys()) {
      Promise.resolve().then(() => api('POST','/turn-ended',{conversation_id:id}))
        .then(result => onOutcome({conversationId:id,result}))
        .catch(() => onOutcome({conversationId:id,result:{outcome:'refused',reason:'turn_ended_unavailable'}}));
    }
  }
  return { register, observe };
}

module.exports = { roomMicHeader, createRoomMicTurns };
