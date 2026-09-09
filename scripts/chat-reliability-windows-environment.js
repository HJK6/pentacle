'use strict';

const inheritedNames = [
  'SystemDrive', 'SystemRoot', 'windir', 'ComSpec', 'Path', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE',
  'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'ProgramData', 'ProgramFiles', 'ProgramFiles(x86)',
  'ProgramW6432', 'CommonProgramFiles', 'CommonProgramFiles(x86)', 'CommonProgramW6432', 'OS',
  'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL', 'PROCESSOR_REVISION',
  'NUMBER_OF_PROCESSORS', 'PENTACLE_RELIABILITY_HOST', 'PENTACLE_RELIABILITY_WIN_WRAPPER',
  'PENTACLE_CONFIG', 'PENTACLE_APP_BIN', 'PENTACLE_WALK_UDD',
];
const wrapperNames = [
  'ELECTRON_RUN_AS_NODE', 'PENTACLE_WALK_NO_BUILD', 'PENTACLE_WALK_ALT', 'PENTACLE_RELIABILITY_NPM',
  'PENTACLE_RELIABILITY_RESULT', 'PENTACLE_RELIABILITY_EXIT', 'PENTACLE_WINDOWS_CHILD',
  'PENTACLE_WINDOWS_READY', 'PENTACLE_WALK_REPO', 'PENTACLE_VSWHERE', 'PENTACLE_VS_INSTALL',
  'npm_config_loglevel', 'npm_config_audit', 'npm_config_fund',
];
const vsDevCmdNames = [
  '__DOTNET_ADD_64BIT', '__DOTNET_PREFERRED_BITNESS', '__VSCMD_PREINIT_PATH', 'CommandPromptType', 'DevEnvDir',
  'ExtensionSdkDir', 'EXTERNAL_INCLUDE', 'Framework40Version', 'FrameworkDir', 'FrameworkDir64', 'FrameworkVersion',
  'FrameworkVersion64', 'INCLUDE', 'LIB', 'LIBPATH', 'Path', 'UCRTVersion', 'UniversalCRTSdkDir', 'VCIDEInstallDir',
  'VCINSTALLDIR', 'VCPKG_ROOT', 'VCToolsInstallDir', 'VCToolsRedistDir', 'VCToolsVersion', 'VisualStudioVersion',
  'VS170COMNTOOLS', 'VSCMD_ARG_app_plat', 'VSCMD_ARG_HOST_ARCH', 'VSCMD_ARG_TGT_ARCH', 'VSCMD_VER', 'VSINSTALLDIR',
  'WindowsLibPath', 'WindowsSdkBinPath', 'WindowsSdkDir', 'WindowsSDKLibVersion', 'WindowsSdkVerBinPath', 'WindowsSDKVersion',
];

const fold = (name) => String(name).toUpperCase();
const toNameSet = (names) => new Set(names.map(fold));
const inheritedSet = toNameSet(inheritedNames);
const wrapperSet = toNameSet(wrapperNames);
const vsDevCmdSet = toNameSet(vsDevCmdNames);

function isDeniedCredentialName(name) {
  const upper = fold(name);
  return /(?:^|_)(?:ANTHROPIC|OPENAI|CODEX|CLAUDE|AWS|GITHUB|GH|AZURE|GOOGLE|GCP|NEXUS|ORCHESTRATION|AGENT_ORCH)(?:_|$)/.test(upper)
    || /(?:_TOKEN|_SECRET|_API_KEY|_PASSWORD|_CREDENTIAL)$/.test(upper)
    || /(?:ACCESS_KEY|SECRET_ACCESS_KEY|SESSION_TOKEN)/.test(upper);
}

function classifyEnvironment(env, allowlist = inheritedNames) {
  const allowed = toNameSet(allowlist);
  const classified = { allowed: [], denied: [], other: [] };
  for (const [name, value] of Object.entries(env || {})) {
    const entry = { name, value };
    if (allowed.has(fold(name))) classified.allowed.push(entry);
    else if (isDeniedCredentialName(name)) classified.denied.push(entry);
    else classified.other.push(entry);
  }
  return classified;
}

function buildExplicitEnvironment(env, allowlist) {
  const source = new Map();
  for (const [name, value] of Object.entries(env || {})) source.set(fold(name), value);
  const result = {};
  for (const name of allowlist) {
    const value = source.get(fold(name));
    if (value !== undefined) result[name] = value;
  }
  return result;
}

function exactNameSet(env, names) {
  const actual = toNameSet(Object.keys(env || {}));
  const expected = toNameSet(names);
  return actual.size === expected.size && [...expected].every((name) => actual.has(name));
}

function deniedValueDisposition(value) {
  const text = String(value || '');
  if (!text) return 'empty';
  if (text.length < 8 || new Set(text).size < 3) return 'reject';
  return 'scan';
}

function validateVsDevCmdDelta(before, after) {
  const prior = new Map(Object.entries(before || {}).map(([name, value]) => [fold(name), { name, value }]));
  const next = new Map(Object.entries(after || {}).map(([name, value]) => [fold(name), { name, value }]));
  const permitted = new Set([...inheritedSet, ...wrapperSet, ...vsDevCmdSet]);
  const unexpected = [];
  for (const [name, entry] of next) {
    if (!permitted.has(name)) unexpected.push(entry.name);
    const old = prior.get(name);
    if (old && old.value !== entry.value && !vsDevCmdSet.has(name)) unexpected.push(entry.name);
  }
  return { ok: unexpected.length === 0, unexpected: [...new Set(unexpected)].sort() };
}

module.exports = {
  inheritedNames,
  wrapperNames,
  vsDevCmdNames,
  isDeniedCredentialName,
  classifyEnvironment,
  buildExplicitEnvironment,
  exactNameSet,
  deniedValueDisposition,
  validateVsDevCmdDelta,
};
