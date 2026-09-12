'use strict';
const { configuredAssistantRole } = require('./assistant_role');

// Reuse the main client's full-snapshot handshake and existing correlated chat
// lifecycle. A claim owns one utterance; sendTurn must only create one request.
function createWakeDelivery({ config, getState, api, sendTurn, onStatus = () => {} }) {
  let busy = false;
  let held = null;
  let epoch = 0;
  let generation;
  let muted = false;
  let lastAttemptedId;
  let note = '';
  const role = () => configuredAssistantRole(config.features);
  const enabled = () => config.features?.mic === true && !!role()
    && typeof config.mic?.wakeTargetHost === 'string' && !!config.mic.wakeTargetHost
    && config.chatStream?.snapshot !== false;
  const target = (state) => {
    if (!state?.connected || !Array.isArray(state.sessions)) return null;
    const matches = state.sessions.filter(s => s.role === role()
      && s.host === config.mic.wakeTargetHost && s.status !== 'closed' && !s.closed_at && s.stream_id);
    return matches.length === 1 ? matches[0].stream_id : null;
  };
  function cancel() {
    epoch += 1;
    held = null;
    muted = true;
    note = '';
  }
  function observe(status) {
    const next = status?.wake?.generation;
    if (next && next !== generation) {
      cancel();
      muted = false;
    }
    if (next) generation = next;
    if (status && status.mode !== 'on') cancel();
  }
  function message(status) {
    if (!status?.wake?.enabled || status.mode !== 'on') return '';
    if (!enabled()) return 'Wake delivery is unavailable in this client.';
    return status.wake.error || note || (held ? 'Wake message waiting for the current assistant.'
      : status.capture_origin === 'wake' ? 'Recording for Bart — say over to send.'
        : status.wake.pending_count ? 'Wake message pending.' : 'Say “Hey Bart” to speak; “over” to send.');
  }
  async function tick(status) {
    observe(status);
    if (!enabled() || !status?.wake?.enabled || status.mode !== 'on' || muted || busy) return;
    if (!held && !status.wake.pending_count) return;
    busy = true;
    const current = epoch;
    const valid = () => epoch === current && !muted;
    try {
      const initialTarget = target(await getState());
      if (!valid()) return;
      if (!initialTarget) {
        note = 'Wake message waiting for a connected, unique assistant.';
        return;
      }
      if (!held) {
        const response = await api('POST', '/wake/claim', {});
        if (!valid()) return;
        if (!response || response.error || !Object.hasOwn(response, 'claim')) {
          note = 'Wake claim unconfirmed. Latest claim is available in the local mic service for review.';
          return;
        }
        held = response.claim;
      }
      if (!held) return;
      if (held.generation !== generation || held.id === lastAttemptedId) {
        held = null;
        return;
      }
      // Off/generation and identity are read again after the destructive claim.
      const latestStatus = await api('GET', '/status');
      observe(latestStatus);
      if (!valid() || !latestStatus || latestStatus.mode !== 'on'
        || latestStatus.wake?.generation !== held?.generation) return;
      const latestTarget = target(await getState());
      if (!valid()) return;
      if (!latestTarget || latestTarget !== initialTarget) {
        note = 'Wake message waiting for the current assistant.';
        return;
      }
      const capture = held;
      held = null;
      lastAttemptedId = capture.id;
      note = '';
      // Normal same-request reconnect replay belongs to ChatStore, not here.
      await sendTurn(latestTarget, capture.text);
    } catch (error) {
      note = lastAttemptedId && !held
        ? 'Wake send unconfirmed. Check the chat send status; no new automatic send will be created.'
        : 'Wake delivery waiting for the connection. Latest claimed message stays in memory.';
    } finally {
      busy = false;
      onStatus(message(status));
    }
  }
  return { tick, cancel, message };
}
module.exports = { createWakeDelivery };
