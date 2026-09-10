// Bundle the public shared reducers and the desktop's actual store and view.
import * as PentacleChatCore from 'pentacle-chat-core';
import { ChatStoreController } from './chat_store_controller';
import * as PentacleChatView from './shared_transcript_view';
import * as ChatReliabilityView from './chat_reliability_view';
import * as ChatReconnectReplay from './chat_reconnect_replay';
import { attachDesktopHarness } from './desktop_harness';

const browser = globalThis as typeof globalThis & Record<string, any>;
const store = new ChatStoreController();
function target(streamId: string) {
  const colon = streamId.indexOf(':');
  if (colon < 1) throw new Error('A host-qualified stream is required');
  return { host: streamId.slice(0, colon), session: streamId.slice(colon + 1) };
}
store.setSendBridge(async ({ streamId, text, requestId, optimisticId, attachments }) => {
  const { host, session } = target(streamId);
  return browser.cc.chatSendCorrelated(host, session, text, requestId, optimisticId, attachments);
});
store.setCancelBridge(async ({ streamId }) => {
  const { host, session } = target(streamId);
  return browser.cc.chatInterrupt(host, session);
});
browser.PentacleChatCore = PentacleChatCore;
browser.PentacleChatStore = store;
browser.PentacleChatView = PentacleChatView;
browser.PentacleChatReliability = { ...ChatReliabilityView, ...ChatReconnectReplay };
browser.PentacleHarness = attachDesktopHarness(store, {
  env: typeof process !== 'undefined' ? process.env : {},
});
