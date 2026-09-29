'use strict';

function roomMicHeader(conversationId, text, contract) {
  if (typeof conversationId !== 'string' || !conversationId.trim()
    || conversationId.length > 256 || /[\x00-\x1f\x7f]/.test(conversationId)) return null;
  return `[pentacle-input ${JSON.stringify({origin:'room_mic',conversation_id:conversationId,...(contract ? {voice_reply:contract}: {})})}]\n\n${text}`;
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
    // A conversation may own several answer roots; delivery identity is the key.
    pending.set(JSON.stringify([streamId, optimisticId]), {streamId,conversationId,optimisticId,requestId,text});
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
      const candidates = [...pending].filter(([, turn]) => {
        if (turn.streamId !== streamId) return false;
        if (event.request_id || event.optimistic_id) {
          if (event.request_id && event.request_id !== turn.requestId) return false;
          if (event.optimistic_id && event.optimistic_id !== turn.optimisticId) return false;
          return true;
        }
        return event.text === turn.text;
      });
      const match = candidates.length === 1 ? candidates[0] : null;
      if (match) {
        pending.delete(match[0]);
        const timestamp = Date.parse(event.timestamp);
        Promise.resolve().then(() => api('POST','/conversation/timing',{
          conversation_id:match[1].conversationId,stage:'delivered_at',
          at:Number.isFinite(timestamp) ? timestamp/1000 : Date.now()/1000,
        })).catch(() => {});
      }
      // A provider can absorb more than one USER root into a single turn. Only
      // the first root owns the uncorrelated terminal; later roots remain open
      // to the service ceiling. Missing fallback is safer than premature closure.
      // Typed roots occupy that same order, but never call the voice endpoint.
      if (!active.has(streamId)) active.set(streamId, {turn:match?.[1],
        requestId:event.request_id,optimisticId:event.optimistic_id});
      return;
    }
    const final = ['ASSIST','ASSIST_TEXT'].includes(kind) && providerRoot && (
      raw.transport === 'codex-rollout' && raw.phase === 'final_answer'
      || raw.transport === 'claude-jsonl' && raw.stop_reason === 'end_turn');
    if (!final && !isSystemEndOfTurnEvent(event)) return;
    const root = active.get(streamId);
    if (!root) return;
    active.delete(streamId); // Remove before I/O: replay never creates another call.
    if (!root.turn || event.request_id && event.request_id !== root.requestId
      || event.optimistic_id && event.optimistic_id !== root.optimisticId) return;
    const id = root.turn.conversationId;
    Promise.resolve().then(() => api('POST','/turn-ended',{conversation_id:id}))
      .then(result => onOutcome({conversationId:id,result}))
      .catch(() => onOutcome({conversationId:id,result:{outcome:'refused',reason:'turn_ended_unavailable'}}));
  }
  return { register, observe };
}

module.exports = { roomMicHeader, createRoomMicTurns };
