const test = require('node:test');
const assert = require('node:assert/strict');

function createGateControl({ setGate = async () => ({ ok: true }), refresh = () => {} } = {}) {
  const view = {
    batch: 'sample-1',
    gate: 'closed',
    selectedBatch: 'sample-1',
    missing: false,
    disabled: false,
    label: 'Unlock',
    subtitle: 'Items waiting for approval',
    error: '',
    pending: false,
    optimistic: null,
  };

  function paint() {
    if (view.missing) {
      view.disabled = true;
      view.label = 'Loading';
      view.subtitle = 'Waiting for selected sample status...';
      return;
    }
    view.disabled = view.pending;
    view.label = view.pending ? (view.gate === 'closed' ? 'Unlocking...' : 'Locking...') : (view.gate === 'closed' ? 'Unlock' : 'Lock');
    view.subtitle = view.gate === 'closed' ? 'Items waiting for approval' : 'New submissions are enabled';
  }

  function update(data) {
    view.batch = data.batch || view.batch;
    view.selectedBatch = data.selectedBatch || view.selectedBatch;
    view.missing = data.missing === true;
    if (view.missing) {
      paint();
      return;
    }
    if (view.optimistic && !view.pending && data.gate === view.optimistic.value) view.optimistic = null;
    if (!view.pending && (!view.optimistic || data.gate === view.optimistic.value)) view.gate = data.gate;
    if (view.optimistic && Date.now() > view.optimistic.expiresAt) {
      view.optimistic = null;
      view.gate = data.gate;
    }
    paint();
  }

  async function commit(next) {
    view.pending = true;
    paint();
    const result = await setGate(view.batch, next);
    if (result && result.ok === false) {
      view.error = ' ' + String(result.error || 'unable to update gate');
      view.pending = false;
      paint();
      return result;
    }
    view.gate = next;
    view.optimistic = { value: next, expiresAt: Date.now() + 30000 };
    view.pending = false;
    paint();
    refresh();
    return result;
  }

  return {
    view,
    update,
    click() {
      if (view.disabled) return null;
      if (view.gate === 'closed') return { confirm: () => commit('open') };
      return commit('closed');
    },
  };
}

test('closed gate shows an unlock action', () => {
  const control = createGateControl();
  control.update({ batch: 'sample-1', gate: 'closed' });
  assert.equal(control.view.label, 'Unlock');
  assert.equal(control.view.disabled, false);
});

test('open gate shows a lock action', () => {
  const control = createGateControl();
  control.update({ batch: 'sample-1', gate: 'open' });
  assert.equal(control.view.label, 'Lock');
  assert.equal(control.view.subtitle, 'New submissions are enabled');
});

test('missing selected sample disables the action', () => {
  const calls = [];
  const control = createGateControl({ setGate: async (...args) => calls.push(args) });
  control.update({ batch: 'sample-1', gate: 'open', missing: true, selectedBatch: 'missing' });
  assert.equal(control.view.disabled, true);
  assert.equal(control.view.label, 'Loading');
  assert.equal(control.click(), null);
  assert.deepEqual(calls, []);
});

test('unlock confirmation calls the action once and refreshes', async () => {
  const calls = [];
  let refreshes = 0;
  const control = createGateControl({
    setGate: async (...args) => {
      calls.push(args);
      return { ok: true };
    },
    refresh: () => { refreshes += 1; },
  });
  const modal = control.click();
  assert.ok(modal);
  assert.deepEqual(calls, []);
  await modal.confirm();
  assert.deepEqual(calls, [['sample-1', 'open']]);
  assert.equal(refreshes, 1);
  assert.equal(control.view.label, 'Lock');
});

test('pending updates keep the control disabled and visible', async () => {
  let resolve;
  const control = createGateControl({ setGate: () => new Promise((done) => { resolve = done; }) });
  const modal = control.click();
  const pending = modal.confirm();
  assert.equal(control.view.disabled, true);
  assert.equal(control.view.label, 'Unlocking...');
  resolve({ ok: true });
  await pending;
  assert.equal(control.view.disabled, false);
  assert.equal(control.view.label, 'Lock');
});

test('failed actions expose an inline error and retryable state', async () => {
  const control = createGateControl({ setGate: async () => ({ ok: false, error: 'sample failure' }) });
  const result = await control.click().confirm();
  assert.equal(result.ok, false);
  assert.equal(control.view.error, ' sample failure');
  assert.equal(control.view.disabled, false);
});
