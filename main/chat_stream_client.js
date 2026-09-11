'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const WebSocket = require('ws');
const perf = require('./perf_telemetry');
const {
  SOURCE_STATE_TO_UI,
  SCHEDULE_LIFECYCLE_EVENT_SOURCE_STATE,
  projectScheduleRow,
  projectScheduleInventory,
  scheduleLifecycleEventType,
} = require('./schedule_projection');

const OPERATOR_AUTH_V2_PREFIX = 'pentacle-auth-v2:';
const OPERATOR_AUTH_V2_SCHEME = 'hmac-sha256-v2';
const OPERATOR_AUTH_V2_CLIENT_KIND = 'pentacle';
const OPERATOR_AUTH_V2_WELCOME_TIMEOUT_MS = 5000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const { nullLimits, validatedLimits, limitsHealthFromFrame } = require('./limits_contract');

function operatorAuthError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function decodeBase64Url(value, expectedBytes, code = 'operator_auth_v2_invalid') {
  if (typeof value !== 'string' || !value || value.includes('=') || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    throw operatorAuthError(code);
  }
  const decoded = Buffer.from(value, 'base64url');
  if (decoded.toString('base64url') !== value || (expectedBytes !== undefined && decoded.length !== expectedBytes)) {
    throw operatorAuthError(code);
  }
  return decoded;
}

function parseDesktopAuthV2Envelope(value) {
  if (typeof value !== 'string' || !value.startsWith(OPERATOR_AUTH_V2_PREFIX)) {
    throw operatorAuthError('operator_auth_v2_invalid');
  }
  let record;
  try {
    record = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(
      decodeBase64Url(value.slice(OPERATOR_AUTH_V2_PREFIX.length)),
    ));
  } catch (_) {
    throw operatorAuthError('operator_auth_v2_invalid');
  }
  if (!record || typeof record !== 'object' || Array.isArray(record)
    || Object.keys(record).sort().join(',') !== 'client_kind,credential_id,proof_key,version'
    || record.version !== 2
    || record.client_kind !== OPERATOR_AUTH_V2_CLIENT_KIND
    || typeof record.credential_id !== 'string'
    || !UUID_PATTERN.test(record.credential_id)) {
    throw operatorAuthError('operator_auth_v2_invalid');
  }
  const proofKey = decodeBase64Url(record.proof_key, 32);
  const canonical = OPERATOR_AUTH_V2_PREFIX + Buffer.from(JSON.stringify({
    client_kind: OPERATOR_AUTH_V2_CLIENT_KIND,
    credential_id: record.credential_id,
    proof_key: proofKey.toString('base64url'),
    version: 2,
  })).toString('base64url');
  if (canonical !== value) throw operatorAuthError('operator_auth_v2_invalid');
  return { credentialId: record.credential_id, proofKey };
}

function createDesktopAuthV2Hello(credential, welcome) {
  const operator = welcome?.auth?.operator;
  if (!operator || operator.protocol_version !== 2 || operator.scheme !== OPERATOR_AUTH_V2_SCHEME
    || typeof operator.expires_at !== 'number' || !Number.isFinite(operator.expires_at)
    || operator.expires_at <= Date.now() / 1000) {
    throw operatorAuthError('operator_auth_v2_required');
  }
  decodeBase64Url(operator.nonce, 32, 'operator_auth_v2_required');
  const transcript = `pentacle-operator-v2\0chat-streamd\0${operator.nonce}\0${credential.credentialId}\0${OPERATOR_AUTH_V2_CLIENT_KIND}`;
  return {
    scheme: OPERATOR_AUTH_V2_SCHEME,
    credential_id: credential.credentialId,
    proof: crypto.createHmac('sha256', credential.proofKey).update(transcript, 'utf8').digest('base64url'),
  };
}

function assetMessage(type, args) {
  const a = args && typeof args === 'object' ? args : {};
  const payload = { type };
  if (a.stream_id !== undefined) payload.stream_id = a.stream_id;
  if (a.streamId !== undefined) payload.stream_id = a.streamId;
  if (a.host !== undefined) payload.host = a.host;
  if (a.session_name !== undefined) payload.session_name = a.session_name;
  if (a.sessionName !== undefined) payload.session_name = a.sessionName;
  if (a.asset_id !== undefined) payload.asset_id = a.asset_id;
  if (a.assetId !== undefined) payload.asset_id = a.assetId;
  if (a.spec_id !== undefined) payload.spec_id = a.spec_id;
  if (a.specId !== undefined) payload.spec_id = a.specId;
  if (a.spec_ids !== undefined) payload.spec_ids = a.spec_ids;
  if (a.specIds !== undefined) payload.spec_ids = a.specIds;
  if (a.from_stream_id !== undefined) payload.from_stream_id = a.from_stream_id;
  if (a.fromStreamId !== undefined) payload.from_stream_id = a.fromStreamId;
  if (a.caller_stream_id !== undefined) payload.caller_stream_id = a.caller_stream_id;
  if (a.callerStreamId !== undefined) payload.caller_stream_id = a.callerStreamId;
  if (a.stream_token !== undefined) payload.stream_token = a.stream_token;
  if (a.streamToken !== undefined) payload.stream_token = a.streamToken;
  return payload;
}

function _backoff(attempt) {
  const base = Math.min(30, [1, 2, 5, 10, 30][Math.min(attempt, 4)]);
  const jitter = base * Math.random() * 0.30;
  return (base + jitter) * 1000;
}

class ChatStreamClient {
  constructor() {
    this._ws = null;
    this._cfg = null;
    this._destroyed = false;
    this._reconnectAttempt = 0;
    this._events = [];
    this._drafts = {};
    this._sessions = [];
    this._schedules = [];
    this._limits = nullLimits();
    this._limitsHealth = null;
    this._hostsStats = {};
    this._recentLimit = 500;
    // Bound the report/asset read RPCs so an unresponsive daemon yields a
    // visible error instead of an infinite loading spinner (75 KB replies land
    // in ~30 ms; the largest report in the store is ~823 KB). Overridable in
    // tests. See sendCommand(timeoutMs).
    this._assetRpcTimeoutMs = 20000;
    // Bound the spawn-catalog read the same way. A slow-consumer client that is
    // 1011-dropped mid-reply (the daemon inventory-broadcast overflow) never
    // drains its spawn_catalog_get reply, so an unbounded pending would spin the
    // New Chat dialog forever. On expiry the loader's in-flight clears and the
    // request stays retryable. Overridable in tests.
    this._spawnCatalogRpcTimeoutMs = 15000;
    this._pending = new Map();
    this._streamEventChunks = new Map();
    this._livenessProbeInFlight = false;
    this._fetchBlobChunks = new Map();
    this._reconnectTimer = null;
    this._welcomeTimer = null;
    this._pendingHandshakeLimitsUpdate = null;
    this._heartbeatTimer = null;
    this._heartbeatMs = 30000;
    this._isAlive = false;
    this.connected = false;
    this._connectionError = null;
    this._stateVersion = 0;
    this._handshakeState = 'idle';
    this._emitFrame = null;
    this._socketGeneration = 0;
    this._buildSha = String(require('../package.json').pentacleBuildSha || '').trim();
    if (!/^[0-9a-f]{40}$/.test(this._buildSha)) {
      try {
        this._buildSha = execFileSync('git', ['-C', path.resolve(__dirname, '..'), 'rev-parse', 'HEAD'], {
          encoding: 'utf8',
        }).trim();
      } catch (_) {
        this._buildSha = '';
      }
    }
  }

  init(cfg, emitFrame) {
    this._cfg = cfg || {};
    this._emitFrame = typeof emitFrame === 'function' ? emitFrame : null;
    this._recentLimit = Math.min(500, this._cfg.chatStream?.recentLimit || 500);
    const raw = Number(this._cfg?.chatStream?.heartbeatMs ?? 30000);
    this._heartbeatMs = Math.min(120000, Math.max(5000, Number.isFinite(raw) ? raw : 30000));
    this._connect();
  }

  snapshot() {
    return {
      connected: this.connected,
      error: this._connectionError,
      state_version: this._stateVersion,
      events: this._events.slice(-500),
      drafts: { ...this._drafts },
      sessions: this._sessions.slice(),
      schedules: this._schedules.slice(),
      limits: validatedLimits(this._limits),
      limits_health: this._limitsHealth,
      hosts_stats: { ...this._hostsStats },
    };
  }

  destroy() {
    this._destroyed = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._clearWelcomeTimer();
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
    this._rejectPending('Chat stream client stopped');
    if (this._ws) {
      try { this._ws.terminate(); } catch (_) {}
    }
  }

  forceReconnect(reason) {
    if (this._destroyed) return;
    const ws = this._ws;
    if (!ws) return;
    if (reason) console.log('[ChatStream] forceReconnect:', reason);
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      // Peer presumed dead, no close frame.
      ws.terminate();
    }
  }

  _readToken() {
    const streamCfg = this._cfg.chatStream || {};
    const inline = String(streamCfg.token || '').trim();
    if (inline) return inline;
    try {
      const tokenPath = path.resolve('~/.config/pentacle-stream/token'.replace(/^~/, os.homedir()));
      return fs.readFileSync(tokenPath, 'utf8').trim();
    } catch (_) {}
    return '';
  }

  _assertNoSymlinkParent(tokenPath) {
    const parsed = path.parse(tokenPath);
    const parentParts = path.relative(parsed.root, path.dirname(tokenPath)).split(path.sep).filter(Boolean);
    let current = parsed.root;
    for (const part of parentParts) {
      current = path.join(current, part);
      if (fs.lstatSync(current).isSymbolicLink()) throw operatorAuthError('operator_auth_v2_private_path_required');
    }
  }

  _windowsPrivateTokenProbe(tokenPath) {
    const script = String.raw`
$ErrorActionPreference = 'Stop'
$tokenPath = [Environment]::GetEnvironmentVariable('PENTACLE_OPERATOR_TOKEN_PATH', 'Process')
if ([string]::IsNullOrWhiteSpace($tokenPath)) { exit 1 }
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Security.AccessControl;
public static class PentacleAuthHandle {
  [StructLayout(LayoutKind.Sequential)] public struct AttributeTag { public UInt32 Attributes; public UInt32 Tag; }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern SafeFileHandle CreateFile(string path, UInt32 access, UInt32 share, IntPtr security, UInt32 creation, UInt32 flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int type, out AttributeTag info, UInt32 size);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern UInt32 GetFileType(SafeFileHandle handle);
  [DllImport("advapi32.dll")]
  static extern UInt32 GetSecurityInfo(SafeFileHandle handle, int objectType, UInt32 securityInfo,
    out IntPtr owner, out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
  [DllImport("advapi32.dll")]
  static extern UInt32 GetSecurityDescriptorLength(IntPtr descriptor);
  [DllImport("kernel32.dll")]
  static extern IntPtr LocalFree(IntPtr memory);
  public static SafeFileHandle OpenNoReparse(string path) {
    SafeFileHandle handle = CreateFile(path, 0x80000000, 0x00000001, IntPtr.Zero, 3, 0x00200080, IntPtr.Zero);
    if (handle.IsInvalid) throw new System.ComponentModel.Win32Exception();
    return handle;
  }
  public static UInt32 AttributesFor(SafeFileHandle handle) {
    AttributeTag info;
    if (!GetFileInformationByHandleEx(handle, 9, out info, (UInt32)Marshal.SizeOf(typeof(AttributeTag)))) throw new System.ComponentModel.Win32Exception();
    return info.Attributes;
  }
  public static bool IsRegularDiskFile(SafeFileHandle handle) { return GetFileType(handle) == 1; }
  public static bool HasOnlyAllowedDacl(SafeFileHandle handle, string currentSid) {
    IntPtr owner, group, dacl, sacl, descriptor;
    UInt32 result = GetSecurityInfo(handle, 1, 0x00000004, out owner, out group, out dacl, out sacl, out descriptor);
    if (result != 0 || descriptor == IntPtr.Zero) return false;
    try {
      UInt32 length = GetSecurityDescriptorLength(descriptor);
      if (length == 0 || length > Int32.MaxValue) return false;
      byte[] bytes = new byte[(int)length];
      Marshal.Copy(descriptor, bytes, 0, bytes.Length);
      RawAcl acl = new RawSecurityDescriptor(bytes, 0).DiscretionaryAcl;
      if (acl == null) return false;
      bool hasCurrentAllow = false;
      foreach (GenericAce ace in acl) {
        QualifiedAce qualified = ace as QualifiedAce;
        if (qualified == null || qualified.AceQualifier != AceQualifier.AccessAllowed) continue;
        string sid = qualified.SecurityIdentifier == null ? null : qualified.SecurityIdentifier.Value;
        if (sid != currentSid && sid != "S-1-5-18" && sid != "S-1-5-32-544") return false;
        if (sid == currentSid) hasCurrentAllow = true;
      }
      return hasCurrentAllow;
    } finally { LocalFree(descriptor); }
  }
}
'@
$handle = [PentacleAuthHandle]::OpenNoReparse($tokenPath)
$stream = [System.IO.FileStream]::new($handle, [System.IO.FileAccess]::Read, 4096, $false)
try {
  if (([PentacleAuthHandle]::AttributesFor($stream.SafeFileHandle) -band 0x400) -ne 0) { exit 1 }
  if (!([PentacleAuthHandle]::IsRegularDiskFile($stream.SafeFileHandle))) { exit 1 }
  $current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if (!([PentacleAuthHandle]::HasOnlyAllowedDacl($stream.SafeFileHandle, $current))) { exit 1 }
  $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true, 4096, $true)
  try { [Console]::Out.Write('OK:' + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($reader.ReadToEnd()))) } finally { $reader.Dispose() }
} finally { $stream.Dispose(); $handle.Dispose() }
`;
    return execFileSync('powershell.exe', [
      '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script,
    ], {
      encoding: 'utf8',
      env: { ...process.env, PENTACLE_OPERATOR_TOKEN_PATH: tokenPath },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
  }

  _readWindowsPrivateToken(tokenPath) {
    try {
      const result = String(this._windowsPrivateTokenProbe(tokenPath) || '').trim();
      const encoded = result.startsWith('OK:') ? result.slice(3) : '';
      if (!encoded || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw operatorAuthError('operator_auth_v2_private_path_required');
      const token = Buffer.from(encoded, 'base64');
      if (token.toString('base64') !== encoded) throw operatorAuthError('operator_auth_v2_private_path_required');
      return token.toString('utf8').trim();
    } catch (error) {
      if (error?.code === 'operator_auth_v2_private_path_required') throw error;
      throw operatorAuthError('operator_auth_v2_private_path_required');
    }
  }

  _readPrivateToken(tokenPath) {
    const resolved = path.resolve(String(tokenPath).replace(/^~/, os.homedir()));
    let before;
    try {
      before = fs.lstatSync(resolved);
    } catch (error) {
      if (error?.code === 'ENOENT') return '';
      throw operatorAuthError('operator_auth_v2_private_path_required');
    }
    if (process.platform === 'win32') return this._readWindowsPrivateToken(resolved);
    let descriptor;
    try {
      if (!before.isFile() || before.isSymbolicLink()) throw operatorAuthError('operator_auth_v2_private_path_required');
      this._assertNoSymlinkParent(resolved);
      descriptor = fs.openSync(resolved, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      const opened = fs.fstatSync(descriptor);
      if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino) {
        throw operatorAuthError('operator_auth_v2_private_path_required');
      }
      const mode = opened.mode & 0o777;
      const uid = typeof process.getuid === 'function' ? process.getuid() : undefined;
      const parent = fs.realpathSync(path.dirname(resolved));
      const parentMode = fs.statSync(parent).mode & 0o777;
      if (mode !== 0o600 || (uid !== undefined && opened.uid !== uid) || parentMode & 0o077) {
        throw operatorAuthError('operator_auth_v2_private_path_required');
      }
      const after = fs.lstatSync(resolved);
      if (!after.isFile() || after.isSymbolicLink() || after.dev !== opened.dev || after.ino !== opened.ino) {
        throw operatorAuthError('operator_auth_v2_private_path_required');
      }
      return fs.readFileSync(descriptor, 'utf8').trim();
    } catch (error) {
      if (error?.code === 'operator_auth_v2_private_path_required') throw error;
      throw operatorAuthError('operator_auth_v2_private_path_required');
    } finally {
      if (descriptor !== undefined) {
        try { fs.closeSync(descriptor); } catch (_) {}
      }
    }
  }

  _authMaterial() {
    const streamCfg = this._cfg?.chatStream || {};
    const inline = String(streamCfg.token || '').trim();
    const explicitTokenPath = String(streamCfg.tokenPath || '').trim();
    let value = inline;
    let privatePath = false;
    if (!value && explicitTokenPath) {
      value = this._readPrivateToken(explicitTokenPath);
      privatePath = true;
    } else if (!value) {
      value = this._readToken();
    }
    if (value.startsWith(OPERATOR_AUTH_V2_PREFIX)) {
      if (!privatePath) throw operatorAuthError('operator_auth_v2_private_path_required');
      return { kind: 'v2', credential: parseDesktopAuthV2Envelope(value) };
    }
    return { kind: 'v1', token: value };
  }

  _helloPayload(welcome, material = this._authMaterial()) {
    // include_subagents:true so the client receives nested/hidden sessions
    // alongside default ones; the renderer's sidebar filter relies on having
    // visibility metadata for every tmux session chat_streamd knows about.
    // events_mode:'summary' opts out of the hello-time recent-events bundle
    // (up to recent_limit per stream × every stream); per-stream events are
    // fetched on demand via requestStreamEvents when chat view opens.
    const payload = {
      type: 'hello',
      client: 'pentacle',
      build_sha: this._buildSha,
      subscribe: this._helloSubscribePayload(),
    };
    if (material.kind === 'v2') {
      payload.auth_v2 = createDesktopAuthV2Hello(material.credential, welcome);
    } else if (material.token) {
      payload.token = material.token;
    }
    return payload;
  }

  _helloSubscribePayload() {
    const cfg = this._cfg || {};
    const chatStream = cfg.chatStream || {};
    const subscribe = { include_subagents: true, events_mode: 'summary' };
    const localHostId = chatStream.hostMap && chatStream.hostMap.local;

    if (chatStream.openedByLocal === true && localHostId) {
      subscribe.opened_by_host_ids = [localHostId];
    }
    if (chatStream.snapshot === false) subscribe.snapshot = false;

    const excludeEventTypes = [];
    if (cfg.features && cfg.features.usage === false) {
      excludeEventTypes.push('limits.update');
    }
    if (excludeEventTypes.length > 0) {
      subscribe.exclude_event_types = excludeEventTypes;
    }

    if (!subscribe.opened_by_host_ids && !subscribe.exclude_event_types && subscribe.snapshot !== false) {
      return { all: true, include_subagents: true, events_mode: 'summary' };
    }
    return subscribe;
  }

  _requestId(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  _rejectPending(errorMessage) {
    for (const [requestId, pending] of this._pending.entries()) {
      this._streamEventChunks.delete(requestId);
      pending.reject({
        type: `${pending.prefix}.error`,
        request_id: requestId,
        error: errorMessage,
      });
    }
    this._pending.clear();
  }

  _handleFetchBlobChunk(msg) {
    if (!msg || msg.type !== 'fetch_blob.chunk' || typeof msg.request_id !== 'string') return false;
    const requestId = msg.request_id;
    const entry = this._fetchBlobChunks.get(requestId) || {
      blob_sha: msg.blob_sha,
      size_bytes: msg.size_bytes,
      chunks: [],
    };
    if (typeof msg.content_b64 === 'string') entry.chunks.push(msg.content_b64);
    if (msg.blob_sha) entry.blob_sha = msg.blob_sha;
    if (Number.isFinite(Number(msg.size_bytes))) entry.size_bytes = Number(msg.size_bytes);
    this._fetchBlobChunks.set(requestId, entry);
    return true;
  }

  _handleCommandResponse(msg) {
    if (msg?.type === 'request_stream_events.chunk' && typeof msg.request_id === 'string') {
      const chunks = this._streamEventChunks.get(msg.request_id) || [];
      if (Array.isArray(msg.events)) chunks.push(...msg.events);
      this._streamEventChunks.set(msg.request_id, chunks);
      return true;
    }
    if (!msg || typeof msg.request_id !== 'string') return false;
    const type = String(msg.type || '');
    // `close` replies with `close.already_closed` (success) or an honest
    // `close.failed` (row left open); both must settle the pending promise.
    const isSendReceipt = type === 'send.result' || type === 'send.indeterminate';
    const isOk = type.endsWith('.ok') || type.endsWith('.already_closed') || isSendReceipt;
    const isError = type.endsWith('.error') || type.endsWith('.failed');
    if (!isOk && !isError) return false;
    const pending = this._pending.get(msg.request_id);
    if (!pending) return false;
    this._pending.delete(msg.request_id);
    if (type === 'request_stream_events.ok') {
      const chunks = this._streamEventChunks.get(msg.request_id);
      this._streamEventChunks.delete(msg.request_id);
      if (chunks?.length) msg.events = [...chunks, ...(Array.isArray(msg.events) ? msg.events : [])];
    }
    if (type === 'fetch_blob.ok') {
      const chunks = this._fetchBlobChunks.get(msg.request_id);
      if (chunks) {
        this._fetchBlobChunks.delete(msg.request_id);
        if (!msg.content_b64) msg.content_b64 = chunks.chunks.join('');
        if (!msg.blob_sha && chunks.blob_sha) msg.blob_sha = chunks.blob_sha;
        if (!msg.size_bytes && Number.isFinite(chunks.size_bytes)) msg.size_bytes = chunks.size_bytes;
      }
    }
    if (isSendReceipt) this._forwardFrame(msg);
    if (isError) {
      this._streamEventChunks.delete(msg.request_id);
      this._fetchBlobChunks.delete(msg.request_id);
      pending.reject(msg);
    } else {
      const session = msg.session || msg;
      const resolved = type === 'spawn.ok' && msg.state
        ? { ...session, state: msg.state } : session;
      pending.resolve(pending.rawResponse ? msg : resolved);
    }
    return true;
  }

  sendCommand(payload, prefix, options = {}) {
    const ws = this._ws;
    if (!this.connected || !ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject({ type: `${prefix}.error`, error: 'Chat stream is not connected' });
    }
    // Phase 5 (desktop_chat_ui_mobile_parity): let the caller OWN the request_id
    // so the renderer's optimistic reducer can correlate send.result frames back
    // to its optimistic_id. Backward-compatible: absent → main generates one as
    // before, so every legacy caller (and existing tests) is unchanged.
    const request_id = (options && typeof options.requestId === 'string' && options.requestId)
      ? options.requestId
      : this._requestId(prefix);
    // A finite timeoutMs bounds the pending promise so a daemon that never
    // sends a matching reply (dropped/mismatched frame, unresponsive satellite,
    // a handler that raises before replying) surfaces a rejection the caller
    // can render — instead of hanging the pending forever (the report/asset tab
    // "loading spinner that never resolves"). The timer is cleared on any
    // settle (reply via _handleReply, disconnect via _rejectPending, or the
    // send throw below), so it never fires against an already-resolved request.
    const timeoutMs = Number.isFinite(options?.timeoutMs) && options.timeoutMs > 0
      ? options.timeoutMs : null;
    return new Promise((resolve, reject) => {
      let timer = null;
      const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };
      const settleResolve = (value) => { clear(); resolve(value); };
      const settleReject = (error) => { clear(); reject(error); };
      this._pending.set(request_id, {
        prefix, resolve: settleResolve, reject: settleReject,
        rawResponse: options?.rawResponse === true,
      });
      if (timeoutMs) {
        timer = setTimeout(() => {
          if (!this._pending.has(request_id)) return;
          this._pending.delete(request_id);
          this._streamEventChunks.delete(request_id);
          this._fetchBlobChunks.delete(request_id);
          reject({ type: `${prefix}.error`, request_id, error: 'timed_out',
            message: `${prefix} timed out after ${timeoutMs}ms` });
        }, timeoutMs);
        if (typeof timer.unref === 'function') timer.unref();
      }
      try {
        ws.send(JSON.stringify({ ...payload, request_id }));
      } catch (e) {
        clear();
        this._pending.delete(request_id);
        reject({ type: `${prefix}.error`, request_id, error: e.message || String(e) });
      }
    });
  }

  spawnSession({ host, provider, model, effort, spawnProfile, catalogVersion, resolutionSource, openedByHostId } = {}) {
    const payload = { type: 'spawn', host, provider };
    // A desktop manual spawn is always a complete, daemon-validated tuple.
    // Keep legacy agent-orch callers working by only adding the V2 fields when
    // the caller supplies the desktop profile.
    if (spawnProfile) {
      payload.schema = 'SpawnRequestV2';
      payload.spawn_profile = spawnProfile;
      payload.model = model;
      payload.effort = effort;
      payload.catalog_version = catalogVersion;
      payload.resolution_source = resolutionSource;
    }
    if (openedByHostId) payload.opened_by_host_id = openedByHostId;
    return this.sendCommand(payload, 'spawn');
  }

  getSpawnCatalog() {
    return this.sendCommand({ type: 'spawn_catalog_get' }, 'spawn_catalog_get', { timeoutMs: this._spawnCatalogRpcTimeoutMs });
  }

  sendMessage({ host, sessionName, text, requestId, optimisticId, attachments } = {}) {
    // requestId (optional) flows through to sendCommand so a renderer-owned
    // optimistic send_id is used on the wire. Absent → legacy main-generated id.
    const payload = { type: 'send', host, session_name: sessionName, text };
    if (typeof optimisticId === 'string' && optimisticId) payload.optimistic_id = optimisticId;
    if (Array.isArray(attachments) && attachments.length > 0) payload.attachments = attachments;
    this.noteInteraction();
    return this.sendCommand(
      payload,
      'send',
      { requestId },
    );
  }

  async uploadBlob({ data, sizeHintBytes, requestId } = {}) {
    const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data || []);
    const uploadRequestId = typeof requestId === 'string' && requestId ? requestId : this._requestId('upload');
    await this.sendCommand(
      { type: 'upload_blob_init', size_hint_bytes: Number.isFinite(Number(sizeHintBytes)) ? Number(sizeHintBytes) : buffer.length },
      'upload_blob.init',
      { requestId: uploadRequestId },
    );
    return this.sendCommand(
      { type: 'upload_blob_chunk', data_b64: buffer.toString('base64'), final: true },
      'upload_blob',
      { requestId: uploadRequestId },
    );
  }

  fetchBlob({ blobSha, requestId } = {}) {
    return this.sendCommand(
      { type: 'fetch_blob', blob_sha: String(blobSha || '') },
      'fetch_blob',
      { requestId },
    );
  }

  // B3 (chat_send_turn_lifecycle_batch2): interrupt the RUNNING turn for a
  // session (daemon injects Escape into the agent pane — the ESC-in-terminal
  // equivalent). msg_id is optional; the desktop chat send path does not carry
  // one, so the interrupt is driven purely by host+session.
  interruptMessage({ host, sessionName, msgId } = {}) {
    const payload = { type: 'send.interrupt', host, session_name: sessionName };
    if (typeof msgId === 'number' && Number.isFinite(msgId)) payload.msg_id = msgId;
    return this.sendCommand(payload, 'send.interrupt');
  }

  // Settle an agent-asked question (claude AskUserQuestion selector). Optional
  // text is the shared-builder answer message; absent text is Cancel-only.
  dismissQuestion({ host, sessionName, questionKey, text } = {}) {
    const payload = {
      type: 'question.dismiss',
      host,
      session_name: sessionName,
      question_key: questionKey,
    };
    if (text !== undefined) payload.text = text;
    return this.sendCommand(payload, 'question.dismiss');
  }

  renameSession({ host, sessionName, displayName, source } = {}) {
    const payload = { type: 'rename', host, session_name: sessionName, display_name: displayName };
    if (source) payload.source = source;
    return this.sendCommand(payload, 'rename');
  }

  async scheduleGet(scheduleId) {
    const reply = await this.sendCommand(
      { type: 'schedule.get', schedule_id: String(scheduleId || '') },
      'schedule',
    );
    const raw = reply?.schedule || reply;
    let schedule = projectScheduleRow(raw, { includePrompt: true, applyTerminalGrace: false });
    if (schedule.initial_prompt_blob_sha && !schedule.initial_prompt_b64) {
      const blob = await this.fetchBlob({ blobSha: schedule.initial_prompt_blob_sha });
      if (typeof blob?.content_b64 !== 'string') {
        throw new Error('schedule_prompt_blob_missing_content');
      }
      schedule = { ...schedule, initial_prompt_b64: blob.content_b64, prompt_storage: 'blob' };
    }
    return reply?.schedule ? { ...reply, schedule } : schedule;
  }

  async scheduleCancel(scheduleId) {
    const reply = await this.sendCommand(
      { type: 'schedule.cancel', schedule_id: String(scheduleId || '') },
      'schedule',
      { requestId: crypto.randomUUID() },
    );
    return this._projectScheduleReply(reply);
  }

  async scheduleRun(scheduleId) {
    const reply = await this.sendCommand(
      { type: 'schedule.run', schedule_id: String(scheduleId || '') },
      'schedule',
      { requestId: crypto.randomUUID() },
    );
    return this._projectScheduleReply(reply);
  }

  async scheduleReschedule(scheduleId, firesAtUtc) {
    const reply = await this.sendCommand({
      type: 'schedule.reschedule',
      schedule_id: String(scheduleId || ''),
      fires_at_utc: String(firesAtUtc || ''),
    }, 'schedule', { requestId: crypto.randomUUID() });
    return this._projectScheduleReply(reply);
  }

  closeSession({ host, sessionName, operatorConfirm, operator_confirm, force }) {
    const payload = { type: 'close', host, session_name: sessionName };
    // operator_confirm must be opt-in from the caller, not defaulted here —
    // the daemon's _close_identity_check assumes the desktop trash-button
    // is the only operator-of-last-resort surface among pentacle callers.
    if (operatorConfirm === true || operator_confirm === true) payload.operator_confirm = true;
    if (force === true) payload.force = true;
    return this.sendCommand(payload, 'close');
  }

  async requestStreamEvents({ streamId, limit, beforeDaemonSeq, chunkLimit } = {}) {
    const stream_id = String(streamId || '');
    if (!stream_id) {
      return { ok: false, error: 'streamId is required' };
    }
    const payload = { type: 'request_stream_events', stream_id };
    if (Number.isFinite(limit)) payload.limit = Number(limit);
    if (Number.isFinite(beforeDaemonSeq)) payload.before_daemon_seq = Number(beforeDaemonSeq);
    if (Number.isFinite(chunkLimit)) payload.chunk_limit = Number(chunkLimit);
    try {
      const reply = await this.sendCommand(payload, 'request_stream_events');
      const events = Array.isArray(reply?.events) ? reply.events : [];
      const merged = this._mergeStreamEvents(events);
      if (events.length > 0) {
        this._forwardFrame({ type: 'stream_events', stream_id, events });
      }
      const exhausted = Number.isFinite(limit) && events.length < Number(limit);
      return { ok: true, stream_id, count: merged, exhausted, nextBeforeDaemonSeq: events.length ? Math.min(...events.map((event) => Number(event?.daemon_seq)).filter(Number.isFinite)) : null };
    } catch (err) {
      return { ok: false, error: err?.error || err?.message || String(err) };
    }
  }

  _mergeStreamEvents(events) {
    if (!Array.isArray(events) || events.length === 0) return 0;
    const seen = new Set();
    for (const existing of this._events) {
      const seq = existing?.daemon_seq;
      if (Number.isFinite(seq)) seen.add(seq);
    }
    let added = 0;
    for (const event of events) {
      const seq = event?.daemon_seq;
      if (Number.isFinite(seq)) {
        if (seen.has(seq)) continue;
        seen.add(seq);
      } else {
        // Daemon broadcasts always carry daemon_seq; absence is unexpected.
        // Insert without dedup; the _recentLimit cap still bounds the ring.
        console.warn('[ChatStream] event missing daemon_seq during merge:', event?.stream_id);
      }
      this._events.push(event);
      added += 1;
    }
    if (added > 0) {
      this._events.sort((a, b) => Number(a?.daemon_seq || 0) - Number(b?.daemon_seq || 0));
      if (this._events.length > this._recentLimit) {
        this._events.splice(0, this._events.length - this._recentLimit);
      }
    }
    return added;
  }

  killSessionRpc({ agentId, host, sessionName }) {
    const payload = { type: 'kill' };
    if (agentId) payload.agent_id = agentId;
    if (host) payload.host = host;
    if (sessionName) payload.session_name = sessionName;
    return this.sendCommand(payload, 'kill');
  }

  // ── Specs subsystem RPCs ──────────────────────────────────────
  // The daemon emits `specs.list.ok` etc. on success, and `specs.error`
  // (single type, request_id routed) on failure. The base sendCommand
  // resolves to `msg.session || msg`; specs responses have no `.session`
  // field so that fallback returns the full payload — including
  // `error_code` and any per-error extras — which is what specs.js needs.

  specsList(filter) {
    const payload = { type: 'specs.list' };
    if (filter && typeof filter === 'object') payload.filter = filter;
    return this.sendCommand(payload, 'specs.list');
  }

  specsGet(specId) {
    return this.sendCommand({ type: 'specs.get', spec_id: String(specId || '') }, 'specs.get');
  }

  specsDrive(specId, options, callerStreamId) {
    const payload = {
      type: 'specs.drive',
      spec_id: String(specId || ''),
      options: options && typeof options === 'object' ? options : {},
    };
    if (callerStreamId) payload.caller_stream_id = String(callerStreamId);
    return this.sendCommand(payload, 'specs.drive');
  }

  specsCapabilities() {
    return this.sendCommand({ type: 'specs.capabilities' }, 'specs.capabilities');
  }

  // ── Notifications subsystem RPCs ──────────────────────────────
  // The daemon emits `notification.list.ok` / `notification.resolve.ok` /
  // `notification.create.ok` on success and `notification.<verb>.error` on
  // failure. _handleCommandResponse routes by request_id and the `.ok`/`.error`
  // suffix, so the `notification` prefix works regardless of the verb. Replies
  // carry top-level fields (`notifications`, `notification`) and no `.session`,
  // so sendCommand's `msg.session || msg` fallback returns the full payload —
  // which is what notifications.js needs.

  notificationList(args) {
    const payload = { type: 'notification.list' };
    if (args && typeof args === 'object') {
      if (args.states !== undefined) payload.states = args.states;
      if (args.limit !== undefined) payload.limit = args.limit;
    }
    return this.sendCommand(payload, 'notification');
  }

  promptList(args) {
    const payload = { type: 'prompt.list' };
    if (args && typeof args === 'object') {
      if (args.producer_stream_id !== undefined) payload.producer_stream_id = args.producer_stream_id;
      else if (args.producerStreamId !== undefined) payload.producer_stream_id = args.producerStreamId;
      if (args.spec_id !== undefined) payload.spec_id = args.spec_id;
      else if (args.specId !== undefined) payload.spec_id = args.specId;
      if (args.open !== undefined) payload.open = args.open;
      if (args.limit !== undefined) payload.limit = args.limit;
    }
    return this.sendCommand(payload, 'prompt.list');
  }

  notificationResolve(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = {
      type: 'notification.resolve',
      notification_id: a.notification_id,
      action_kind: a.action_kind,
      by: a.by || 'operator',
    };
    if (a.action_id !== undefined && a.action_id !== null) payload.action_id = a.action_id;
    if (a.choice !== undefined) payload.choice = a.choice;
    if (a.selections !== undefined) payload.selections = a.selections;
    if (a.text !== undefined) payload.text = a.text;
    if (a.custom_text !== undefined) payload.custom_text = a.custom_text;
    else if (a.customText !== undefined) payload.custom_text = a.customText;
    if (a.note !== undefined) payload.note = a.note;
    if (a.submit !== undefined) payload.submit = a.submit;
    if (a.spawn !== undefined && a.spawn !== null) payload.spawn = a.spawn;
    return this.sendCommand(payload, 'notification');
  }

  notificationCreate(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = { type: 'notification.create' };
    for (const key of ['producer', 'title', 'body', 'severity', 'dedup_key', 'actions', 'ttl_seconds']) {
      if (a[key] !== undefined) payload[key] = a[key];
    }
    return this.sendCommand(payload, 'notification');
  }

  assetList(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.list', a);
    return this.sendCommand(payload, 'asset');
  }

  assetGet(args) {
    const payload = assetMessage('asset.get', args);
    return this.sendCommand(payload, 'asset', { timeoutMs: this._assetRpcTimeoutMs });
  }

  assetCommentsList(args) {
    const payload = assetMessage('asset.comments.list', args);
    if (args && typeof args === 'object' && args.unresolved !== undefined) payload.unresolved = !!args.unresolved;
    return this.sendCommand(payload, 'asset', { timeoutMs: this._assetRpcTimeoutMs });
  }

  assetCommentAdd(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.comment.add', a);
    for (const key of ['section_id', 'block_id', 'run_index', 'excerpt', 'body', 'author', 'parent_comment_id', 'comment_id']) {
      if (a[key] !== undefined) payload[key] = a[key];
    }
    return this.sendCommand(payload, 'asset');
  }

  assetCommentEdit(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.comment.edit', a);
    if (a.comment_id !== undefined) payload.comment_id = a.comment_id;
    if (a.body !== undefined) payload.body = a.body;
    return this.sendCommand(payload, 'asset');
  }

  assetCommentDelete(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.comment.delete', a);
    if (a.comment_id !== undefined) payload.comment_id = a.comment_id;
    return this.sendCommand(payload, 'asset');
  }

  assetDelete(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.delete', a);
    return this.sendCommand(payload, 'asset');
  }

  assetCommentResolve(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.comment.resolve', a);
    if (a.comment_id !== undefined) payload.comment_id = a.comment_id;
    if (a.resolved !== undefined) payload.resolved = !!a.resolved;
    if (a.resolved_by !== undefined) payload.resolved_by = a.resolved_by;
    if (a.note !== undefined) payload.note = a.note;
    return this.sendCommand(payload, 'asset');
  }

  assetReviewSet(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.review.set', a);
    if (a.review_status !== undefined) payload.review_status = a.review_status;
    if (a.status !== undefined) payload.status = a.status;
    return this.sendCommand(payload, 'asset');
  }

  assetReadSet(args) {
    const a = args && typeof args === 'object' ? args : {};
    const payload = assetMessage('asset.read.set', a);
    if (a.read !== undefined) payload.read = !!a.read;
    return this.sendCommand(payload, 'asset');
  }

  assetCommentsSendToChat(args) {
    return this.sendCommand(assetMessage('asset.comments.send_to_chat', args), 'asset');
  }

  _replaceSchedules(schedules) {
    this._schedules = projectScheduleInventory(schedules);
  }

  _projectScheduleReply(reply) {
    if (!reply?.schedule || typeof reply.schedule !== 'object') return reply;
    return {
      ...reply,
      schedule: projectScheduleRow(reply.schedule, { applyTerminalGrace: false }),
    };
  }

  _applyScheduleEvent(msg) {
    const scheduleId = String(msg?.schedule_id || '');
    if (!scheduleId) return;
    const idx = this._schedules.findIndex((s) => String(s?.schedule_id || '') === scheduleId);
    const current = idx >= 0 ? this._schedules[idx] : {};
    const next = { ...current, ...Object.fromEntries(Object.entries(msg).filter(([key]) => key !== 'type')) };
    const preservesLegacyReason = msg?.__legacy_error_reason === true;
    if (!preservesLegacyReason) delete next.__legacy_error_reason;
    if (!preservesLegacyReason
        && !['error', 'indeterminate'].includes(String(next.state || ''))) {
      delete next.error_reason;
    }
    if (['pending', 'firing', 'retry_pending'].includes(String(next.state || ''))) {
      delete next.terminal_at;
    }
    if (idx >= 0) this._schedules.splice(idx, 1, next);
    else this._schedules.push(next);
  }

  _applyUnderscoreScheduleEvent(msg) {
    // The preserved chat-stream v1 contract predates the v2 inventory shape:
    // it calls the retry state pending_retry and terminal events do not carry
    // terminal_at. Normalize that wire shape at the compatibility boundary so
    // the strict eight-state projection can stay strict for native v2 frames.
    const legacyState = String(msg?.state || '');
    const normalizedLegacyState = legacyState === 'pending_retry' ? 'retry_pending' : legacyState;
    const sourceState = SCHEDULE_LIFECYCLE_EVENT_SOURCE_STATE[String(msg?.type || '')];
    let event = msg;
    if (sourceState && Object.hasOwn(SOURCE_STATE_TO_UI, sourceState)) {
      if (normalizedLegacyState !== sourceState) {
        console.warn(
          `[ChatStream] legacy schedule event/state disagreement type=${msg.type}`
          + ` state=${legacyState || '<missing>'} projected_state=${sourceState}`,
        );
      }
      const scheduleId = String(msg?.schedule_id || '');
      const current = this._schedules.find(
        (schedule) => String(schedule?.schedule_id || '') === scheduleId,
      ) || {};
      const normalized = { ...current, ...msg, state: sourceState };
      if (msg.type === 'schedule_retry_scheduled' && typeof msg?.next_fire_at_utc === 'string') {
        normalized.fires_at_utc = msg.next_fire_at_utc;
      }
      if (['fired', 'cancelled', 'failed', 'indeterminate', 'expired'].includes(sourceState)
          && !normalized.terminal_at) {
        normalized.terminal_at = new Date().toISOString();
      }
      const legacyReason = msg?.last_error_code ?? msg?.error_reason;
      const preservesLegacyReason = [
        'schedule_retry_scheduled',
        'schedule_failed',
        'schedule_expired',
        'schedule_indeterminate',
      ].includes(String(msg?.type || ''))
        && legacyReason !== undefined
        && legacyReason !== null
        && String(legacyReason);
      const projected = projectScheduleRow(normalized, {
        preserveErrorReason: Boolean(preservesLegacyReason),
      });
      if (!Object.hasOwn(msg, 'prompt_preview')) delete projected.prompt_preview;
      if (preservesLegacyReason) projected.__legacy_error_reason = true;
      event = { type: msg.type, ...projected };
    }
    this._applyScheduleEvent(event);
    this._forwardFrame(event);
  }

  _applyScheduleLifecycle(msg) {
    const raw = msg?.schedule && typeof msg.schedule === 'object'
      ? msg.schedule
      : Object.fromEntries(Object.entries(msg || {}).filter(([key]) => ![
        'type', 'event_type', 'event', 'lifecycle', 'mutation',
      ].includes(key)));
    const sourceState = String(raw?.state || '');
    const projected = projectScheduleRow(raw);
    const scheduleId = String(raw?.schedule_id || '');
    if (!projected) {
      this._schedules = this._schedules.filter(
        (schedule) => String(schedule?.schedule_id || '') !== scheduleId,
      );
      this._forwardFrame({ type: 'schedule.inventory', schedules: this._schedules.slice() });
      return;
    }
    const eventType = scheduleLifecycleEventType(msg, sourceState);
    if (!eventType) throw new Error(`schedule_projection_error: lifecycle event missing for ${sourceState}`);
    this._applyScheduleEvent(projected);
    this._forwardFrame({ type: eventType, ...projected });
  }

  _clearWelcomeTimer() {
    if (this._welcomeTimer) {
      clearTimeout(this._welcomeTimer);
      this._welcomeTimer = null;
    }
  }

  _setConnected(connected) {
    const next = !!connected;
    if (this.connected !== next) {
      this.connected = next;
      this._stateVersion += 1;
    }
  }

  _setConnectionError(error) {
    const next = error == null ? null : String(error);
    if (this._connectionError !== next) {
      this._connectionError = next;
      this._stateVersion += 1;
    }
  }

  _completeHandshake(ws) {
    if (ws !== this._ws || this._handshakeState !== 'hello_sent') return;
    this._handshakeState = 'ready';
    this._setConnected(true);
    this._setConnectionError(null);
    this._isAlive = true;
    this._clearWelcomeTimer();
    if (this._heartbeatTimer) clearInterval(this._heartbeatTimer);
    this._heartbeatTimer = setInterval(() => this._heartbeatTick(), this._heartbeatMs);
    fs.mkdirSync(path.join(os.homedir(), '.pentacle'), { recursive: true });
    fs.writeFileSync(path.join(os.homedir(), '.pentacle', 'desktop-runtime.json'), JSON.stringify({
      sha: this._buildSha,
      pid: process.pid,
      connected_at: new Date().toISOString(),
    }) + '\n');
  }

  _queueHandshakeLimitsUpdate(msg) {
    const limits = validatedLimits(msg?.limits);
    if (!limits) return;
    const health = limitsHealthFromFrame(msg);
    if (!health.valid) return;
    this._pendingHandshakeLimitsUpdate = { ...msg, limits, _validatedLimitsHealth: health.value };
  }

  _flushHandshakeLimitsUpdate() {
    const msg = this._pendingHandshakeLimitsUpdate;
    this._pendingHandshakeLimitsUpdate = null;
    if (!msg) return;
    const { _validatedLimitsHealth, ...frame } = msg;
    this._forwardFrame(frame);
    this._limits = msg.limits;
    this._limitsHealth = _validatedLimitsHealth;
  }

  _failHandshake(ws, error) {
    if (ws !== this._ws || this._handshakeState === 'failed') return;
    const message = String(error?.code || error?.message || error || 'operator_auth_v2_required');
    this._handshakeState = 'failed';
    this._pendingHandshakeLimitsUpdate = null;
    this._setConnected(false);
    this._setConnectionError(message);
    this._isAlive = false;
    this._clearWelcomeTimer();
    this._rejectPending(message);
    this._forwardFrame({ connected: false, error: message });
    try { ws.terminate(); } catch (_) {}
    this._scheduleReconnect();
  }

  _push(event) {
    this._events.push(event);
    if (this._events.length > this._recentLimit) {
      this._events.splice(0, this._events.length - this._recentLimit);
    }
  }

  _forwardFrame(msg) {
    if (this._emitFrame) this._emitFrame({ ...msg, state_version: this._stateVersion });
  }

  // User sends are a focused interaction. If a socket has silently missed its
  // prior pong, recover now through the ordinary close/reconnect path instead of
  // letting a new optimistic send wait for the next periodic heartbeat.
  noteInteraction() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    // A prior focused ping has intentionally set _isAlive false while awaiting
    // pong. Coalesce rapid/queued interactions onto that probe rather than
    // treating the transient state as a real half-open socket.
    if (this._livenessProbeInFlight) return true;
    if (!this._isAlive) {
      perf.record('chat-stream:half-open-recovery');
      ws.terminate();
      return false;
    }
    if (!this._livenessProbeInFlight) {
      this._livenessProbeInFlight = true;
      this._isAlive = false;
      perf.record('chat-stream:focused-liveness-probe');
      try { ws.ping(); } catch (_) { this._livenessProbeInFlight = false; }
    }
    return true;
  }

  _connect() {
    if (this._destroyed) return;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    let authMaterial;
    try {
      authMaterial = this._authMaterial();
    } catch (error) {
      const message = String(error?.code || error?.message || 'operator_auth_v2_required');
      this._setConnected(false);
      this._setConnectionError(message);
      this._rejectPending(message);
      this._forwardFrame({ connected: false, error: message });
      this._scheduleReconnect();
      return;
    }
    const url = this._cfg.chatStream?.url || 'ws://127.0.0.1:7791';
    let ws;
    let snapshotDisabled = false;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error('[ChatStream] constructor error:', e.message);
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;
    perf.record('chat-stream:ws-connect-attempt', { url, attempt: this._reconnectAttempt });
    ws.on('open', () => {
      perf.record('chat-stream:ws-open');
      this._setConnected(false);
      this._isAlive = false;
      this._handshakeState = 'await_welcome';
      this._clearWelcomeTimer();
      this._welcomeTimer = setTimeout(() => {
        this._failHandshake(ws, operatorAuthError('operator_auth_v2_required'));
      }, OPERATOR_AUTH_V2_WELCOME_TIMEOUT_MS);
      this._reconnectAttempt = 0;
      const prevGeneration = this._socketGeneration;
      this._socketGeneration += 1;
      this._pendingHandshakeLimitsUpdate = null;
      this._forwardFrame({
        type: '__reconnect',
        generation: prevGeneration,
        next_generation: this._socketGeneration,
      });
      if (prevGeneration > 0) {
        this._limits = nullLimits();
        this._limitsHealth = null;
      }
    });
    ws.on('pong', () => {
      if (ws !== this._ws) return;
      this._isAlive = true;
      this._livenessProbeInFlight = false;
    });
    ws.on('message', (rawData) => {
      if (ws !== this._ws) return;
      let msg;
      try { msg = JSON.parse(rawData.toString()); } catch (_) { return; }
      if (this._handshakeState === 'await_welcome') {
        if (msg.type !== 'welcome') {
          this._failHandshake(ws, operatorAuthError('operator_auth_v2_required'));
          return;
        }
        try {
          const hello = this._helloPayload(msg, authMaterial);
          snapshotDisabled = hello.subscribe?.snapshot === false;
          ws.send(JSON.stringify(hello));
          this._handshakeState = 'hello_sent';
          perf.record('chat-stream:hello-sent');
        } catch (error) {
          this._failHandshake(ws, error);
        }
        return;
      }
      if (msg.type === 'auth.error' || msg.type === 'hello.error') {
        this._failHandshake(ws, msg.error || msg.error_code || 'operator_auth_v2_required');
        return;
      }
      if (this._handshakeState === 'hello_sent') {
        if (msg.type === 'limits.update') {
          this._queueHandshakeLimitsUpdate(msg);
          return;
        }
        if (msg.type === 'snapshot') {
          this._completeHandshake(ws);
        } else if (snapshotDisabled && msg.type === 'ready' && msg.snapshot === false) {
          this._completeHandshake(ws);
          this._flushHandshakeLimitsUpdate();
          return;
        } else {
          return;
        }
      }
      if (this._handshakeState !== 'ready') return;
      if (this._handleFetchBlobChunk(msg)) {
        return;
      }
      if (this._handleCommandResponse(msg)) {
        return;
      }
      if (msg.type === 'ping') {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'pong' }));
        return;
      }
      if (msg.type === 'snapshot') {
        perf.record('chat-stream:snapshot-received', {
          events: Array.isArray(msg.events) ? msg.events.length : 0,
          sessions: Array.isArray(msg.sessions) ? msg.sessions.length : 0,
        });
        this._events = Array.isArray(msg.events) ? msg.events.slice(-this._recentLimit) : [];
        this._drafts = msg.drafts || {};
        this._sessions = Array.isArray(msg.sessions) ? msg.sessions : [];
        this._replaceSchedules(msg.schedules);
        if (Object.prototype.hasOwnProperty.call(msg, 'limits')) {
          const limits = validatedLimits(msg.limits);
          if (limits) {
            const health = limitsHealthFromFrame(msg);
            if (health.valid) {
              this._limits = limits;
              this._limitsHealth = health.value;
            }
          }
        }
        this._forwardFrame({
          ...msg,
          connected: true,
          events: this._events,
          schedules: this._schedules,
        });
        this._flushHandshakeLimitsUpdate();
      } else if (msg.type === 'limits.update') {
        this._forwardFrame(msg);
        const limits = validatedLimits(msg.limits);
        const health = limitsHealthFromFrame(msg);
        if (limits && health.valid) {
          this._limits = limits;
          this._limitsHealth = health.value;
        }
      } else if (msg.type === 'session.inventory' && Array.isArray(msg.sessions)) {
        this._forwardFrame(msg);
        this._sessions = msg.sessions;
      } else if (msg.type === 'schedule.inventory' && Array.isArray(msg.schedules)) {
        this._replaceSchedules(msg.schedules);
        this._forwardFrame({ type: 'schedule.inventory', schedules: this._schedules.slice() });
      } else if (msg.type === 'schedule.lifecycle') {
        this._applyScheduleLifecycle(msg);
      } else if (typeof msg.type === 'string' && msg.type.startsWith('schedule_')) {
        this._applyUnderscoreScheduleEvent(msg);
      } else if (msg.type === 'hosts.stats' && msg.hosts && typeof msg.hosts === 'object') {
        this._forwardFrame(msg);
        this._hostsStats = { ...msg.hosts };
      } else if (msg.type === 'chat.event' && msg.event) {
        this._forwardFrame(msg);
        if (msg.event.kind === 'DRAFT' && msg.event.stream_id) {
          this._drafts[msg.event.stream_id] = msg.event;
        }
        this._push(msg.event);
      } else {
        this._forwardFrame(msg);
      }
    });
    ws.on('close', () => {
      if (ws !== this._ws) return;
      const message = this._handshakeState === 'failed'
        ? (this._connectionError || 'operator_auth_v2_required')
        : 'Stream disconnected';
      this._setConnected(false);
      this._setConnectionError(message);
      this._isAlive = false;
      this._handshakeState = 'idle';
      this._pendingHandshakeLimitsUpdate = null;
      this._clearWelcomeTimer();
      if (this._heartbeatTimer) {
        clearInterval(this._heartbeatTimer);
        this._heartbeatTimer = null;
      }
      this._rejectPending('Stream disconnected');
      this._forwardFrame({ connected: false, error: message });
      this._scheduleReconnect();
    });
    ws.on('error', (err) => {
      if (ws !== this._ws) return;
      console.error('[ChatStream] WS error:', err.message);
    });
  }

  _heartbeatTick() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (!this._isAlive) {
      console.log('[ChatStream] heartbeat timeout — terminating socket');
      ws.terminate();
      return;
    }
    this._isAlive = false;
    ws.ping();
  }

  _scheduleReconnect() {
    if (this._destroyed) return;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    const delay = _backoff(this._reconnectAttempt++);
    this._reconnectTimer = setTimeout(() => {
      if (this._destroyed) return;
      this._connect();
    }, delay);
  }
}

module.exports = new ChatStreamClient();
