'use strict';

const KEY = 'pentacle.next.mic';

function reduceInputs(devices, selection, sawIds) {
  const rows = (devices || []).filter(item => item?.kind === 'audioinput'
    && !['default', 'communications'].includes(item.deviceId));
  const real = new Map();
  for (const row of rows) {
    if (typeof row.deviceId !== 'string' || !row.deviceId) continue;
    const label = typeof row.label === 'string' ? row.label.trim() : '';
    if (!real.has(row.deviceId) || label) real.set(row.deviceId, label);
  }
  const lost = !!selection && (real.size ? !real.has(selection) : rows.length === 0 && sawIds);
  const selected = lost ? null : selection;
  const known = rows.length === 0 || (rows.every(row => real.has(row.deviceId) && real.get(row.deviceId)));
  const inputs = known ? Array.from(real, ([id, label]) => ({id, label})) : [];
  return {
    sawIds: sawIds || real.size > 0,
    selection: selected,
    lost,
    nativeId: inputs.some(input => input.id === selected) ? selected : null,
    options: inputs.length > 1 ? [{id: null, label: 'System default'}, ...inputs] : [],
  };
}

function selectionAfterFailure(selection, requested, name) {
  return selection && selection === requested && ['NotFoundError','OverconstrainedError'].includes(name)
    ? null : selection;
}

function createMicrophonePreference(env, storage, onChange) {
  const media = env.isSecureContext ? env.navigator?.mediaDevices : null;
  let selected = null;
  try { selected = storage?.getItem(KEY) || null; } catch {}
  let model = reduceInputs([], selected, false);
  let devices = [];
  let generation = 0;

  function save(id) {
    selected = id;
    try {
      if (id) storage?.setItem(KEY, id);
      else storage?.removeItem(KEY);
    } catch {}
  }
  function update(rows) {
    devices = rows;
    model = reduceInputs(devices, selected, model.sawIds);
    if (model.lost) save(null);
    onChange?.(model);
  }
  async function refresh() {
    if (typeof media?.enumerateDevices !== 'function') return;
    const id = ++generation;
    try {
      const rows = await media.enumerateDevices();
      if (id === generation && Array.isArray(rows)) update(rows);
    } catch {
      // An enumeration failure has no device-loss meaning.
    }
  }
  function choose(id) {
    if (!model.options.some(option => option.id === id)) return;
    save(id);
    update(devices);
  }
  media?.addEventListener?.('devicechange', refresh);
  void refresh();
  return {
    get view() { return model; },
    choose, refresh,
    recorderOptions: {
      getDeviceId: () => model.nativeId,
      onCaptureStart: refresh,
      // The shared recorder calls this hook only for NotFound/Overconstrained.
      onDeviceFailure(id) {
        const next = selectionAfterFailure(selected, id, 'NotFoundError');
        if (next !== selected) { save(next); update(devices); }
      },
    },
  };
}

module.exports = { reduceInputs, selectionAfterFailure, createMicrophonePreference };
