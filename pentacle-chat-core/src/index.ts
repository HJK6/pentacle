// pentacle-chat-core public API barrel.
// Re-exports every public symbol from the platform-neutral chat-stream
// event/model core modules. `export *` is used so both values and types
// surface; a collision check confirmed there are no duplicate export names
// across these modules.
export * from './types/pentacle';
export * from './services/pentacleEventUtils';
export * from './services/pentacleEventBuckets';
export * from './services/pentacleEventInterpreter';
export * from './services/pentacleHosts';
export * from './services/pentacleEventFlowDiagnostics';
export * from './services/optimisticMatch';
export * from './services/pentacleStreamReducer';
export * from './services/pentacleChatModel';
export * from './services/markdown';
export * from './services/questionAnswerFormat';
export * from './services/questionAnswerFormat.examples';
export * from './services/questionPager';
export * from './utils/telemetry';
export * from './utils/telemetryEvents';

// host configuration injection seam. `getHostOrder` is intentionally NOT re-exported
// from here because `pentacleChatModel` already re-exports it (both names refer
// to the same delegating function); `export *`-ing it from both modules would
// produce an ambiguous re-export. The host installs config via
// `setHostConfigProvider`; `getHostTheme` and the seam types round out the API.
export {
  setHostConfigProvider,
  getHostTheme,
  type HostConfigProvider,
  type HostTheme,
} from './services/hostConfig';

export * from "./services/daemonUpdates";
