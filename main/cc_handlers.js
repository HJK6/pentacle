'use strict';

// ── Shared `window.cc` handler registration ──────────────────────────────────
// One definition, two transports. `main.js` registers these on Electron's
// `ipcMain`; `server/ws_bridge.js` registers the same set into a table and
// dispatches it from websocket frames. Keeping one definition is what stops the
// desktop and the web host from drifting.
//
// Handlers keep their native `(event, ...args)` signature. The web host passes a
// per-connection stand-in for `event.sender`, which is what gives
// `main/terminal_adapter.js` — whose slots are keyed by `event.sender.id` —
// correct per-socket isolation and per-socket pty output for free.
//
// Electron-native handlers are NOT here: they stay in the Electron main-process
// adapters — the clipboard pair in `main/clipboard_ipc_bridge.js`, and
// `open-external`, `meeting:*` and `app:reload` directly in `main.js`. All of
// them are listed below so the websocket dispatcher can refuse them with a
// reason.

const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

const { registerAssetIpcHandlers } = require('./asset_ipc_bridge');
const { registerScheduleIpcHandlers } = require('./schedule_ipc_bridge');
const { registerNotificationIpcHandlers } = require('./notification_ipc_bridge');
const { registerTerminalIpc } = require('./terminal_adapter');
const { isProtectedAssistantRename } = require('./assistant_role_guard');
const { probeMicServer } = require('./mic-url');

// Channels the browser answers itself rather than sending to the host. The
// clipboard is the important one: a round trip would read the SERVER's
// clipboard, not the viewer's. The websocket dispatcher refuses these, so a
// shim that forgets to implement one fails loudly instead of silently reading
// the wrong machine.
// Each entry carries the channel's preload mode, because a refusal must obey
// the same contract as a real dispatch: answering a `send` channel would push
// an unsolicited frame at a caller that never asked for one.
const WEB_LOCAL = {
  'clipboard:read-text': { mode: 'invoke', reason: 'the viewer\'s own clipboard, via navigator.clipboard' },
  'clipboard:write-text': { mode: 'invoke', reason: 'the viewer\'s own clipboard, via navigator.clipboard' },
  'open-external': { mode: 'invoke', reason: 'the browser opens the tab itself' },
  'app:reload': { mode: 'send', reason: 'location.reload()' },
};

// Channels with no host-side browser equivalent: the host still refuses them
// (a safety net if anything reaches this transport), but the *web layer*
// (renderer/web_cc.js) now answers each with a browser behavior — the reason
// records what that is. `context-menu` renders an HTML menu that fires the same
// assign-slot/action events; `meeting:open` toasts. They stay listed here so the
// parity test keeps pinning main.js's native registrations.
const WEB_UNSUPPORTED = {
  'context-menu': { mode: 'send', reason: 'native Menu popup — web: HTML context menu (web_cc.js) firing assign-slot/action' },
  'meeting:open': { mode: 'send', reason: 'native window — web: "not available in web mode" toast' },
  'meeting:close': { mode: 'send', reason: 'native window — web: no-op' },
};

// Main → renderer push channels. Electron sends them through
// `event.sender.send`; the web host sends `{event, args}` frames.
const PUSH_EVENTS = [
  'pty:data',
  'pty:exit',
  'assign-slot',
  'action',
  'chat-stream:frame',
  'asset-popout:init',
  'asset:dock',
  'chat:popout-dock',
];

// Channels `preload.js` declares that NO main-process handler serves, on the
// desktop as much as on the web. These are pre-existing gaps in the public
// tree, not web-mode regressions — invoking one rejects with "No handler
// registered" in Electron today. They are listed so the parity test pins the
// set: a new dangling method fails the build, and implementing one here is what
// removes it from this list.
//
// Lane 2 disposition (parity pass): the two chat-popout channels are deferred to
// a web-mode popout follow-up spec (browser popout would be window.open of a
// served route, matching the desktop BrowserWindow) — annotate, do not implement
// here. The six dashboard channels have no provider in the public desktop, so
// there is nothing to shim; they stay annotated until a provider exists.
const UNIMPLEMENTED = {
  'chat-stream:chat-pop-out': 'no handler on either transport; web popout (window.open) deferred to the web-mode popout follow-up spec',
  'chat-stream:chat-dock': 'no handler on either transport; paired with chat-pop-out, deferred to the web-mode popout follow-up spec',
  'dashboard:pipeline-stats': 'no dashboard provider in the public desktop',
  'dashboard:business-stats': 'no dashboard provider in the public desktop',
  'dashboard:pentacle-mobile-testing-stats': 'no dashboard provider in the public desktop',
  'dashboard:set-batch-gate': 'no dashboard provider in the public desktop',
  'dashboard:0dte-stats': 'no dashboard provider in the public desktop',
  'dashboard:0dte-list-traders': 'no dashboard provider in the public desktop',
};

// Handlers with no `window.cc` caller. `dashboard:list` is the board-discovery
// channel the renderer does not consume yet.
const UNCALLED = {
  'dashboard:list': 'registered for board discovery; no window.cc method calls it yet',
};

// `window.cc` methods with no IPC channel at all.
const PRELOAD_LOCAL = {
  chatPopoutContext: 'parsed from argv (Electron) / the query string (web)',
};

function safeString(value, fallback = '') { return String(value ?? '').trim() || fallback; }
function nowIso() { return new Date().toISOString(); }
function normalizeChatStreamError(error) { return String(error?.error || error?.message || error || 'Daemon unavailable'); }
function resultError(message) { return { ok: false, error: normalizeChatStreamError(message) }; }

async function command(action) {
  try { return { ok: true, ...await action() }; }
  catch (error) { return { ...resultError(error), code: error?.code, remediation: error?.remediation }; }
}

/**
 * Build the portable half of the `window.cc` surface.
 *
 * `register(target)` takes anything with `handle`/`on` — Electron's `ipcMain`,
 * or the websocket collector — and returns the terminal adapter's stop
 * function, which the caller wires to its own shutdown.
 */
function createCcHandlers({
  CONFIG,
  chatStreamClient,
  assetPopouts = null,
  configError = null,
  configWarnings = [],
  harness = process.env.PENTACLE_HARNESS === '1',
  terminalOptions = undefined,
}) {
  const telemetry = [];

  function publicConfig() {
    const { token, tokenPath, ...chatStream } = CONFIG.chatStream || {};
    return { ...CONFIG, chatStream, hostIds: CONFIG.chatStream?.hosts || ['local'], platform: process.platform,
      hostname: os.hostname(), isClient: Boolean(CONFIG.remote), configError: configError?.message || null, configWarnings };
  }

  function protectedAssistantRenameError(host, sessionName) {
    return isProtectedAssistantRename(CONFIG, chatStreamClient.snapshot(), host, sessionName)
      ? resultError('This configured assistant cannot be renamed')
      : null;
  }

  function register(target) {
    target.handle('get-config', () => publicConfig());

    target.handle('chat-stream:get-state', () => chatStreamClient.snapshot());
    target.handle('chat-stream:spawn-catalog', () => command(async () => ({ catalog: await chatStreamClient.getSpawnCatalog() })));
    target.handle('chat-stream:spawn', async (_event, request, legacyHostId) => {
      const input = request && typeof request === 'object' ? request : { provider: request, host: legacyHostId };
      const spawnProfile = input.spawnProfile || input.spawn_profile;
      if (spawnProfile === 'desktop_manual' && (!input.model || !input.effort)) return resultError('A model and effort are required');
      return command(async () => {
        const response = await chatStreamClient.spawnSession({ ...input, host: input.hostId || input.host || 'local', spawnProfile });
        if (response?.state === 'queued') return { ...response, streamId: response.stream_id };
        const session = response.session || response;
        return { ...response, session, streamId: response.stream_id || session?.stream_id,
          requested: session?.requested_launch_tuple, resolved: session?.resolved_launch_tuple,
          actualLaunch: session?.actual_launch_tuple };
      });
    });
    target.handle('chat-stream:send', (_event, host, sessionName, text, requestId, optimisticId, attachments) =>
      command(async () => {
        const receipt = await chatStreamClient.sendMessage({ host, sessionName, text, requestId, optimisticId, attachments });
        return { ...receipt, ok: receipt.delivery === 'landed' || receipt.action_committed === true,
          ...(receipt.delivery === 'not_landed' && !receipt.action_committed ? { error: receipt.reason || 'Message was not delivered' } : {}) };
      }));
    target.handle('chat-stream:request-stream-events', (_event, args) => command(() => chatStreamClient.requestStreamEvents(args)));
    target.handle('chat-stream:interrupt', (_event, host, sessionName) => command(() => chatStreamClient.interruptMessage({ host, sessionName })));
    target.handle('chat-stream:dismiss-question', (_event, host, sessionName, payload = {}) =>
      command(() => chatStreamClient.dismissQuestion({ host, sessionName, questionKey: payload.questionKey || payload.question_key, text: payload.text })));
    target.handle('chat-stream:rename', (_event, host, sessionName, displayName) => {
      const protectedError = protectedAssistantRenameError(host, sessionName);
      return protectedError || command(() => chatStreamClient.renameSession({ host, sessionName, displayName, source: 'manual' }));
    });
    target.handle('chat-stream:close', (_event, host, sessionName, options) => command(() => chatStreamClient.closeSession({ ...options, host, sessionName })));
    target.handle('chat-stream:kill', (_event, args) => command(() => chatStreamClient.killSessionRpc(args)));
    target.handle('chat-stream:upload-blob', (_event, payload) => command(() => chatStreamClient.uploadBlob({ ...payload,
      data: typeof payload?.data === 'string' ? Buffer.from(payload.data, 'base64') : payload?.data })));
    target.handle('chat-stream:fetch-blob', (_event, blobSha) => command(() => chatStreamClient.fetchBlob({ blobSha })));

    registerAssetIpcHandlers(target, chatStreamClient, normalizeChatStreamError, assetPopouts);
    registerScheduleIpcHandlers(target, chatStreamClient, normalizeChatStreamError);
    registerNotificationIpcHandlers(target, chatStreamClient, normalizeChatStreamError);

    target.handle('tmux:kill-session', (_event, host, sessionName) => command(() => chatStreamClient.killSessionRpc({ host, sessionName })));
    target.handle('tmux:set-window-title', (_event, host, sessionName, displayName) => {
      const protectedError = protectedAssistantRenameError(host, sessionName);
      return protectedError || command(() => chatStreamClient.renameSession({ host, sessionName, displayName, source: 'manual' }));
    });
    if (harness) target.handle('harness:force-reconnect', () => { chatStreamClient.forceReconnect('harness'); return { ok: true }; });

    const stopTerminals = registerTerminalIpc(target, CONFIG, chatStreamClient, terminalOptions);

    target.handle('pty:save-image', (_event, base64Data) => {
      try {
        const value = Buffer.from(safeString(base64Data), 'base64');
        const filePath = path.join(os.tmpdir(), 'desktop-paste-' + Date.now() + '.png');
        fs.writeFileSync(filePath, value);
        return { ok: true, path: filePath };
      } catch {
        return resultError('could not save image');
      }
    });

    target.handle('dashboard:list', () => ({ ok: true, boards: [] }));
    target.handle('ui-review:list-artifacts', () => []);
    target.handle('specs:list', (_event, filter) => command(() => chatStreamClient.specsList(filter)));
    target.handle('specs:get', (_event, id) => command(() => chatStreamClient.specsGet(id)));
    target.handle('specs:drive', (_event, id, options, caller) => command(() => chatStreamClient.specsDrive(id, options, caller)));
    target.handle('specs:capabilities', () => command(() => chatStreamClient.specsCapabilities()));

    // Microphone service ownership stays external to the public desktop.
    target.handle('mic:start-server', () => probeMicServer(CONFIG));

    target.on('perf-telemetry:record', (_event, value) => {
      if (telemetry.length >= 1000) telemetry.shift();
      telemetry.push({ at: nowIso(), event: safeString(value && value.event, 'unknown') });
    });
    target.handle('perf-telemetry:state', () => ({
      enabled: false,
      log_path: null,
      buffered_events: telemetry.length,
    }));

    return stopTerminals;
  }

  return { register, publicConfig, telemetry };
}

/**
 * A duck-typed `ipcMain` that records registrations into a table instead of
 * binding them. `mode` mirrors `preload.js`: 'invoke' answers, 'send' does not.
 */
function createCollector(table = {}) {
  return {
    table,
    handle(channel, fn) { table[channel] = { mode: 'invoke', handler: fn }; },
    on(channel, fn) { table[channel] = { mode: 'send', handler: fn }; },
  };
}

module.exports = {
  createCcHandlers,
  createCollector,
  WEB_LOCAL,
  WEB_UNSUPPORTED,
  UNIMPLEMENTED,
  UNCALLED,
  PUSH_EVENTS,
  PRELOAD_LOCAL,
  safeString,
  normalizeChatStreamError,
  resultError,
  command,
};
