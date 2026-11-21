'use strict';

const ACCOUNT_PROVIDERS = new Set(['claude', 'codex']);
const SPAWN_PROFILE = 'desktop_manual';
const DEFAULT_FAILURE = 'Failed to start chat';

function catalogFrom(input) {
  if (input?.catalog && typeof input.catalog === 'object') return input.catalog;
  return input && typeof input === 'object' ? input : {};
}

function modelCatalogFor(catalog) {
  const available = catalog?.available_models;
  if (available && typeof available === 'object' && !Array.isArray(available)) return available;
  const models = catalog?.models;
  return models && typeof models === 'object' && !Array.isArray(models) ? models : {};
}

function modelMapFor(catalog, provider) {
  return modelCatalogFor(catalog)[provider] || {};
}

function providerDisplayName(provider) {
  const normalized = String(provider || '').trim().toLowerCase();
  if (normalized === 'codex') return 'Codex';
  if (normalized === 'claude') return 'Claude';
  if (normalized === 'gemini') return 'Gemini';
  if (normalized === 'cursor') return 'Cursor';
  return provider ? String(provider).trim() : 'Agent';
}

function profileFor(catalog, provider) {
  const profile = catalog.profiles?.[SPAWN_PROFILE]?.[provider];
  return Array.isArray(profile) ? profile : null;
}

function normalizeMachineOptions(options = {}) {
  const supplied = options.machineOptions || options.machines || options.hostIds;
  const candidates = Array.isArray(supplied) && supplied.length
    ? supplied
    : [options.localHostId || options.localHost || 'local'];

  const seen = new Set();
  return candidates.flatMap((entry) => {
    const idValue = typeof entry === 'string'
      ? entry
      : (entry?.id ?? entry?.hostId ?? entry?.value ?? '');
    const id = String(idValue || '').trim();
    if (!id || seen.has(id)) return [];
    seen.add(id);
    const labelValue = typeof entry === 'string'
      ? entry
      : (entry?.label ?? entry?.name ?? id);
    return [{ id, label: String(labelValue || id) }];
  });
}

function accountDisplayName(account, labels, provider) {
  const alias = String(account?.alias || '');
  const configured = labels?.[provider]?.[alias];
  if (typeof configured === 'string' && configured.trim()) return configured.trim().split('@')[0];
  return alias;
}

function singleLine(value) {
  const line = String(value ?? '').replace(/[\r\n\t ]+/g, ' ').trim();
  return line || DEFAULT_FAILURE;
}

function sessionStreamId(session) {
  const value = session?.stream_id ?? session?.streamId;
  return value == null ? '' : String(value);
}

function sessionStatus(session) {
  return String(session?.state || session?.bootstrap_state || '').toLowerCase();
}

function findSession(sessions, streamId) {
  if (!streamId || !Array.isArray(sessions)) return null;
  return sessions.find(session => sessionStreamId(session) === streamId) || null;
}

function responseStreamId(response) {
  const value = response?.streamId
    ?? response?.stream_id
    ?? response?.session?.stream_id
    ?? response?.session?.streamId;
  return value == null || value === '' ? null : String(value);
}

function createNewChatForm(options = {}) {
  const catalog = catalogFrom(options.catalog);
  const accountLabels = options.accountLabels || {};
  const machines = normalizeMachineOptions(options);
  const preferredMachine = String(options.localHostId || options.localHost || '');
  const initialMachine = machines.some(machine => machine.id === preferredMachine)
    ? preferredMachine
    : (machines[0]?.id || '');
  const spawn = options.spawn;
  const onAdmitted = options.onAdmitted;
  const onStarted = options.onStarted;
  const listeners = new Set();

  const state = {
    phase: 'form',
    visible: true,
    provider: null,
    model: null,
    account: null,
    effort: null,
    machine: initialMachine,
    error: '',
    streamId: null,
    admittedStreamIds: [],
    session: null,
  };

  let latestSessions = [];
  let admitted = false;
  let listedAfterAdmission = false;
  let lastSessionReason = '';

  function providers() {
    const profiles = catalog.profiles?.[SPAWN_PROFILE] || {};
    const models = modelCatalogFor(catalog);
    return Object.keys(models).flatMap((provider) => {
      const profile = profiles[provider];
      const providerModels = models[provider] || {};
      const modelCount = Object.keys(providerModels).length;
      if (!Array.isArray(profile) || !modelCount) return [];
      return [{ id: provider, label: providerDisplayName(provider), modelCount }];
    });
  }

  function modelIds(provider = state.provider) {
    if (!provider || !profileFor(catalog, provider)) return [];
    return Object.keys(modelMapFor(catalog, provider));
  }

  function accountsFor(provider = state.provider, machineId = state.machine) {
    if (!provider || !machineId || !ACCOUNT_PROVIDERS.has(provider)) return [];
    const rows = catalog.account_status?.accounts;
    if (!Array.isArray(rows)) return [];
    return rows.flatMap((account) => {
      if (account?.host !== machineId || account?.provider !== provider
        || !account.active || !account.enabled || account.health !== 'healthy') return [];
      const alias = String(account.alias || '');
      const label = accountDisplayName(account, accountLabels, provider);
      if (!alias || !label) return [];
      return [{ id: alias, label }];
    });
  }

  function defaultAccountAlias(provider = state.provider, machineId = state.machine) {
    if (!provider || !machineId || !ACCOUNT_PROVIDERS.has(provider)) return null;
    const defaults = catalog.account_status?.defaults;
    if (!Array.isArray(defaults)) return null;
    const alias = defaults.find(row => row?.host === machineId && row?.provider === provider)?.alias;
    return alias && accountsFor(provider, machineId).some(account => account.id === alias)
      ? String(alias) : null;
  }

  function effortsFor(provider = state.provider, model = state.model) {
    const efforts = modelMapFor(catalog, provider)?.[model]?.efforts;
    return Array.isArray(efforts) ? efforts.slice() : [];
  }

  function usualEffort(provider, model) {
    const profile = profileFor(catalog, provider);
    const efforts = effortsFor(provider, model);
    if (!efforts.length) return null;
    const providerEffort = profile?.[1];
    return efforts.includes(providerEffort) ? providerEffort : efforts[0];
  }

  function canStart() {
    return state.phase === 'form' && state.visible
      && !!state.provider && !!state.model && !!state.effort
      && !!state.machine
      && modelIds().includes(state.model)
      && effortsFor().includes(state.effort)
      && (!state.account || accountsFor().some(account => account.id === state.account))
      && machines.some(machine => machine.id === state.machine);
  }

  function getChoices() {
    return {
      providers: providers(),
      models: modelIds(),
      accounts: accountsFor(),
      efforts: effortsFor(),
      machines: machines.map(machine => ({ ...machine })),
    };
  }

  function getState() {
    return {
      ...state,
      admittedStreamIds: state.admittedStreamIds.slice(),
      session: state.session ? { ...state.session } : null,
      canStart: canStart(),
      controlsDisabled: state.phase === 'waiting',
    };
  }

  function getViewModel() {
    return { ...getState(), choices: getChoices() };
  }

  function notify() {
    const viewModel = getViewModel();
    for (const listener of listeners) listener(viewModel);
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') return () => {};
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function select(fieldName, value) {
    const field = fieldName === 'host' ? 'machine' : fieldName;
    if (!state.visible || state.phase !== 'form') return false;
    const selected = String(value ?? '');
    let changed = false;

    if (field === 'provider' && providers().some(provider => provider.id === selected)) {
      if (state.provider === selected) return false;
      state.provider = selected;
      state.model = null;
      state.effort = null;
      state.account = defaultAccountAlias(selected, state.machine);
      changed = true;
    } else if (field === 'model' && modelIds().includes(selected)) {
      if (state.model === selected) return false;
      state.model = selected;
      state.effort = usualEffort(state.provider, selected);
      changed = true;
    } else if (field === 'account' && accountsFor().some(account => account.id === selected)) {
      if (state.account === selected) return false;
      state.account = selected;
      changed = true;
    } else if (field === 'effort' && effortsFor().includes(selected)) {
      if (state.effort === selected) return false;
      state.effort = selected;
      changed = true;
    } else if (field === 'machine' && machines.some(machine => machine.id === selected)) {
      if (state.machine === selected) return false;
      const previousDefault = defaultAccountAlias(state.provider, state.machine);
      const previousAccount = state.account;
      state.machine = selected;
      const nextAccounts = accountsFor(state.provider, selected);
      state.account = previousAccount && previousAccount !== previousDefault
        && nextAccounts.some(account => account.id === previousAccount)
        ? previousAccount
        : defaultAccountAlias(state.provider, selected);
      changed = true;
    }

    if (!changed) return false;
    state.error = '';
    state.session = null;
    notify();
    return true;
  }

  function spawnRequest() {
    const profile = profileFor(catalog, state.provider);
    const defaultAlias = catalog.account_status?.defaults?.find(
      row => row?.host === state.machine && row?.provider === state.provider,
    )?.alias;
    const request = {
      hostId: state.machine,
      provider: state.provider,
      model: state.model,
      effort: state.effort,
      spawnProfile: SPAWN_PROFILE,
      catalogVersion: catalog.catalog_version,
      resolutionSource: state.model === profile?.[0] && state.effort === profile?.[1]
        ? 'profile_default'
        : 'explicit_override',
    };
    if (state.account && state.account !== defaultAlias) request.account = state.account;
    return request;
  }

  function notifyAdmitted(reply) {
    if (typeof onAdmitted !== 'function') return;
    onAdmitted({ streamId: state.streamId, reply, state: getState() });
  }

  function restoreForm(reason, session = null) {
    state.phase = 'form';
    state.error = singleLine(reason || lastSessionReason);
    state.session = session ? { ...session } : (state.session ? { ...state.session } : null);
    admitted = false;
    notify();
  }

  function started(session) {
    if (state.phase !== 'waiting') return;
    state.phase = 'started';
    state.error = '';
    state.session = session ? { ...session } : null;
    notify();
    if (state.visible && typeof onStarted === 'function') {
      onStarted({ session: state.session ? { ...state.session } : null, streamId: state.streamId });
    }
  }

  function observeSession(session, { fromList = false } = {}) {
    if (state.phase !== 'waiting' || !session) return;
    state.session = { ...session };
    if (fromList) listedAfterAdmission = true;
    if (typeof session.reason === 'string' && session.reason.trim()) {
      lastSessionReason = session.reason;
    }
    const status = sessionStatus(session);
    if (status === 'failed') {
      restoreForm(lastSessionReason || DEFAULT_FAILURE, session);
      return;
    }
    if (status === 'queued' || status === 'starting') {
      notify();
      return;
    }
    started(session);
  }

  function updateSessions(sessions) {
    latestSessions = Array.isArray(sessions) ? sessions.slice() : [];
    if (state.phase !== 'waiting' || !admitted || !state.streamId) return getState();
    const session = findSession(latestSessions, state.streamId);
    if (session) {
      observeSession(session, { fromList: true });
    } else if (listedAfterAdmission) {
      restoreForm(lastSessionReason || DEFAULT_FAILURE, state.session);
    }
    return getState();
  }

  function leave() {
    if (!state.visible) return getState();
    state.visible = false;
    notify();
    return getState();
  }

  async function start() {
    if (state.phase === 'waiting' || state.phase === 'started' || !canStart()) return getState();
    const request = spawnRequest();
    state.phase = 'waiting';
    state.error = '';
    state.session = null;
    admitted = false;
    listedAfterAdmission = false;
    lastSessionReason = '';
    notify();

    let reply;
    try {
      if (typeof spawn !== 'function') throw new Error('Chat spawning is unavailable');
      reply = await spawn(request);
    } catch (error) {
      restoreForm(error?.message || String(error));
      return getState();
    }

    if (!reply?.ok) {
      restoreForm(reply?.error || DEFAULT_FAILURE);
      return getState();
    }

    admitted = true;
    state.streamId = responseStreamId(reply);
    if (state.streamId && !state.admittedStreamIds.includes(state.streamId)) {
      state.admittedStreamIds.push(state.streamId);
    }
    state.session = reply.session ? { ...reply.session } : null;
    notify();
    notifyAdmitted(reply);

    const currentSession = findSession(latestSessions, state.streamId);
    if (currentSession) listedAfterAdmission = true;
    if (reply.session) {
      observeSession(reply.session);
    } else {
      if (currentSession) observeSession(currentSession, { fromList: true });
    }

    return getState();
  }

  return {
    getState,
    getChoices,
    getViewModel,
    subscribe,
    select,
    start,
    updateSessions,
    leave,
  };
}

function appendChoiceRow(document, container, { field, label, options, selected, disabled, tiles = false }) {
  const row = document.createElement('div');
  row.className = 'srow';
  const heading = document.createElement('div');
  heading.className = 'slabel';
  heading.textContent = label;
  const choices = document.createElement('div');
  choices.className = tiles ? 'sopts tiles' : 'sopts';
  choices.setAttribute('role', 'radiogroup');
  choices.setAttribute('aria-label', label);

  for (const option of options) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `opt${tiles ? ' tile' : ''}${selected === option.id ? ' on' : ''}`;
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', String(selected === option.id));
    button.dataset.set = field;
    button.dataset.val = option.id;
    button.dataset.testid = `set-${field}-${option.id}`;
    button.disabled = disabled;
    if (tiles) {
      const name = document.createElement('b');
      name.textContent = option.label;
      const detail = document.createElement('span');
      detail.textContent = `${option.modelCount} ${option.modelCount === 1 ? 'model' : 'models'}`;
      button.append(name, detail);
    } else {
      button.textContent = option.label;
    }
    choices.appendChild(button);
  }

  row.append(heading, choices);
  container.appendChild(row);
}

function mountNewChatForm(root, options = {}) {
  if (!root?.ownerDocument) throw new TypeError('A form root element is required');
  const document = root.ownerDocument;
  const store = options.store || null;
  const form = createNewChatForm(options);
  let destroyed = false;
  let unsubscribeStore = null;
  const controllerUnsubscribe = form.subscribe(render);

  function render(view = form.getViewModel()) {
    if (destroyed) return;
    if (view.phase === 'started') {
      dispose();
      return;
    }
    if (!view.visible) {
      root.replaceChildren();
      return;
    }

    const page = document.createElement('div');
    page.className = 'page setup';
    page.setAttribute('aria-busy', String(view.phase === 'waiting'));
    const column = document.createElement('div');
    column.className = 'scol';
    const header = document.createElement('div');
    header.className = 'shead';
    const title = document.createElement('h1');
    title.textContent = 'New chat';
    header.appendChild(title);
    column.appendChild(header);

    const disabled = view.phase === 'waiting';
    appendChoiceRow(document, column, {
      field: 'provider', label: 'Provider', options: view.choices.providers,
      selected: view.provider, disabled, tiles: true,
    });
    if (view.provider) {
      appendChoiceRow(document, column, {
        field: 'model', label: 'Model', options: view.choices.models.map(id => ({ id, label: id })),
        selected: view.model, disabled,
      });
      if (view.choices.accounts.length) {
        appendChoiceRow(document, column, {
          field: 'account', label: 'Account', options: view.choices.accounts,
          selected: view.account, disabled,
        });
      }
      if (view.model && view.choices.efforts.length) {
        appendChoiceRow(document, column, {
          field: 'effort', label: 'Effort', options: view.choices.efforts.map(id => ({ id, label: id })),
          selected: view.effort, disabled,
        });
      }
      appendChoiceRow(document, column, {
        field: 'host', label: 'Machine', options: view.choices.machines,
        selected: view.machine, disabled,
      });
    }

    const startButton = document.createElement('button');
    startButton.type = 'button';
    startButton.className = 'start';
    startButton.dataset.act = 'start';
    startButton.dataset.testid = 'start-chat';
    startButton.disabled = disabled || !view.canStart;
    startButton.textContent = disabled ? 'Starting' : 'Start chat';
    column.appendChild(startButton);

    if (view.error) {
      const error = document.createElement('div');
      error.className = 'start-error new-chat-error';
      error.setAttribute('role', 'alert');
      error.textContent = singleLine(view.error);
      column.appendChild(error);
    }

    page.appendChild(column);
    root.replaceChildren(page);
  }

  function handleClick(event) {
    const target = event.target?.closest?.('button');
    if (!target || !root.contains(target) || target.disabled) return;
    if (target.dataset.set) {
      form.select(target.dataset.set, target.dataset.val);
    } else if (target.dataset.act === 'start') {
      void form.start();
    }
  }

  function syncSessions() {
    const sessions = store?.getState?.()?.sessions;
    if (Array.isArray(sessions)) form.updateSessions(sessions);
  }

  function dispose({ leaveForm = false } = {}) {
    if (destroyed) return;
    destroyed = true;
    root.removeEventListener('click', handleClick);
    if (typeof unsubscribeStore === 'function') unsubscribeStore();
    controllerUnsubscribe();
    if (leaveForm) form.leave();
    root.replaceChildren();
  }

  function destroy() {
    dispose({ leaveForm: true });
  }

  root.addEventListener('click', handleClick);
  if (store && typeof store.subscribe === 'function') {
    unsubscribeStore = store.subscribe(syncSessions);
    syncSessions();
  } else if (Array.isArray(options.sessions)) {
    form.updateSessions(options.sessions);
  }
  render();

  return {
    getState: form.getState,
    getChoices: form.getChoices,
    select: form.select,
    start: form.start,
    updateSessions: form.updateSessions,
    destroy,
    leave: destroy,
  };
}

module.exports = {
  DEFAULT_FAILURE,
  createNewChatForm,
  mountNewChatForm,
};
