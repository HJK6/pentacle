'use strict';
function decideUseChatClose(state, streamSession) {
  return !!(state && state.chatStream && state.chatStream.connected && streamSession && streamSession.stream_id);
}
module.exports = { decideUseChatClose };
