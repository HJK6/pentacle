'use strict';

const { durableQuestionNotificationId, durableQuestionOptionBModel } = require('../durable_question_model');
const { filterSidebarSessions } = require('../sidebar_filter');

const RECEIPT_MS = 2600;
const ANSWERED_STATES = new Set(['answered', 'consumed']);

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function questionId(notification) {
  return text(notification?.question?.question_id || notification?.question_id);
}

function questionSpecId(notification) {
  const question = notification?.question || {};
  return text(question.spec_id || question.envelope?.spec_id || notification?.spec_id || notification?.envelope?.spec_id);
}

function questionProducerId(notification) {
  const question = notification?.question || {};
  return text(question.producer_stream_id || question.envelope?.producer_stream_id || notification?.producer_stream_id);
}

function isQuestionOpen(notification) {
  if (!notification || typeof notification !== 'object' || !notification.question) return false;
  if (!questionId(notification)) return false;
  if (notification.producer && notification.producer !== 'agent_question.v1') return false;
  if (notification.state != null && notification.state !== 'open') return false;
  if (notification.question.state != null && notification.question.state !== 'open') return false;
  return !notification.question.answered_at && !notification.question.answer?.at;
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value instanceof Map) return [...value.values()];
  if (Array.isArray(value?.notifications)) return value.notifications;
  if (value && typeof value === 'object') return Object.values(value);
  return [];
}

function uniqueOpenNotifications(notifications) {
  const byId = new Map();
  for (const notification of asArray(notifications)) {
    if (!isQuestionOpen(notification)) continue;
    const id = questionId(notification);
    byId.set(id, notification);
  }
  return [...byId.values()];
}

function currentHeldRead(read, lane) {
  if (!read?.reply) return false;
  const listedRevision = text(lane?.detail_revision);
  const readRevision = text(read?.detailRevision);
  return !listedRevision || readRevision === listedRevision;
}

function readProjection(read) {
  return read?.reply?.projection || read?.reply?.data?.projection || read?.projection || {};
}

function safeRead(laneReads, laneId, kind, goalId) {
  if (typeof laneReads?.getRead !== 'function') return { status: 'idle' };
  try {
    return laneReads.getRead(laneId, kind, goalId) || { status: 'idle' };
  } catch (_) {
    return { status: 'failed' };
  }
}

function listGoals(lane, laneReads) {
  const goals = Array.isArray(lane?.goals) ? [...lane.goals] : [];
  const read = safeRead(laneReads, lane?.lane_id, 'goals');
  if (!currentHeldRead(read, lane)) return { goals, read };
  const detailGoals = readProjection(read).goals;
  if (Array.isArray(detailGoals)) return { goals: detailGoals, read };
  if (Array.isArray(read?.reply?.goals)) return { goals: read.reply.goals, read };
  return { goals, read };
}

function laneMembers(lane, laneReads) {
  const preview = Array.isArray(lane?.progress?.members) ? lane.progress.members : (Array.isArray(lane?.members) ? lane.members : []);
  const read = safeRead(laneReads, lane?.lane_id, 'members');
  const all = new Map();
  for (const member of preview) {
    if (text(member?.spec_id)) all.set(text(member.spec_id), member);
  }
  if (currentHeldRead(read, lane)) {
    const detailMembers = readProjection(read).members;
    if (Array.isArray(detailMembers)) {
      for (const member of detailMembers) {
        if (text(member?.spec_id)) all.set(text(member.spec_id), member);
      }
    }
  }
  const rawTotal = Number(lane?.progress?.members_total ?? lane?.members_total);
  const previewComplete = Number.isFinite(rawTotal) && rawTotal <= preview.length;
  return {
    members: [...all.values()],
    read,
    known: previewComplete || currentHeldRead(read, lane),
  };
}

function questionTitle(notification) {
  return text(notification?.title || notification?.question?.title) || 'Question';
}

function sessionIndex(sessions) {
  const visible = filterSidebarSessions(asArray(sessions));
  const byStream = new Map();
  for (const session of visible) {
    const id = text(session?.stream_id);
    if (id) byStream.set(id, session);
  }
  return byStream;
}

function placementFor(notification, lanes, laneReads, listedSessions) {
  const specId = questionSpecId(notification);
  const producerId = questionProducerId(notification);
  if (notification?.question_identity_pending) return { kind: 'unplaced', retryIdentity: !!notification.question_identity_failed, retryReads: [], retryLaneIds: [] };
  if (!specId) {
    const chat = producerId ? listedSessions.get(producerId) : null;
    if (chat) {
      return {
        kind: 'chat', chatId: producerId,
        chatTitle: text(chat.display_name || chat.title || chat.session_name || chat.name),
        retryReads: [], retryLaneIds: [],
      };
    }
    return { kind: 'title', retryReads: [], retryLaneIds: [] };
  }

  for (const lane of lanes) {
    const memberState = laneMembers(lane, laneReads);
    const member = memberState.members.find((candidate) => text(candidate?.spec_id) === specId);
    if (!member) continue;
    const laneId = text(lane?.lane_id);
    const laneTitle = text(lane?.title);
    const specTitle = text(member?.title);
    const goalId = text(member?.goal_id);
    if (!goalId) {
      return {
        kind: 'lane', laneId, laneTitle, laneIcon: lane?.icon || null, specId, specTitle,
        retryReads: [], retryLaneIds: [],
      };
    }

    const { goals, read } = listGoals(lane, laneReads);
    const goal = goals.find((candidate) => text(candidate?.goal_id) === goalId);
    if (goal) {
      return {
        kind: 'lane_goal', laneId, laneTitle, laneIcon: lane?.icon || null,
        goalId, goalTitle: text(goal.title), specId, specTitle,
        retryReads: [], retryLaneIds: [],
      };
    }

    const retryReads = read?.status === 'failed' ? [{ laneId, kind: 'goals' }] : [];
    return {
      kind: 'unplaced', specId, retryReads, retryLaneIds: retryReads.map((entry) => entry.laneId),
    };
  }

  const retryReads = [];
  for (const lane of lanes) {
    const memberState = laneMembers(lane, laneReads);
    if (!memberState.known && memberState.read?.status === 'failed') {
      retryReads.push({ laneId: text(lane?.lane_id), kind: 'members' });
    }
  }
  return {
    kind: 'unplaced', specId, retryReads,
    retryLaneIds: [...new Set(retryReads.map((entry) => entry.laneId))],
  };
}

function buildQuestionPlacement({ inventory, notifications, sessions, laneReads } = {}) {
  const lanes = Array.isArray(inventory?.lanes) ? inventory.lanes : [];
  const listedSessions = sessionIndex(sessions);
  const questions = uniqueOpenNotifications(notifications).map((notification) => ({
    id: questionId(notification),
    notification,
    model: durableQuestionOptionBModel(notification),
    title: questionTitle(notification),
    specId: questionSpecId(notification),
    producerId: questionProducerId(notification),
    placement: placementFor(notification, lanes, laneReads, listedSessions),
  }));

  const requestedMembers = new Set();
  const requestedGoals = new Set();
  const hasUnlocatedSpec = questions.some((item) => item.specId && !lanes.some((lane) =>
    laneMembers(lane, laneReads).members.some((member) => text(member?.spec_id) === item.specId)));
  if (hasUnlocatedSpec && lanes.length) {
    const searchOrder = lanes.map((lane, index) => ({ lane, index }))
      .sort((a, b) => Number((b.lane.progress?.members || b.lane.members || []).some((member) => Number(member?.open_questions?.count) > 0))
        - Number((a.lane.progress?.members || a.lane.members || []).some((member) => Number(member?.open_questions?.count) > 0)) || a.index - b.index);
    for (const { lane } of searchOrder) {
      const state = laneMembers(lane, laneReads);
      if (!state.known && state.read?.status === 'idle' && text(lane?.lane_id)) requestedMembers.add(text(lane.lane_id));
    }
  }

  for (const item of questions) {
    if (item.placement.kind !== 'unplaced' || !item.specId) continue;
    // A member visible in a preview can require the uncapped goals read even
    // while another question remains unplaced elsewhere.
    for (const lane of lanes) {
      const state = laneMembers(lane, laneReads);
      if (!state.members.some((member) => text(member?.spec_id) === item.specId)) continue;
      const member = state.members.find((candidate) => text(candidate?.spec_id) === item.specId);
      const goalId = text(member?.goal_id);
      if (!goalId) break;
      const goals = listGoals(lane, laneReads);
      if (goals.goals.some((goal) => text(goal?.goal_id) === goalId)) break;
      if (goals.read?.status === 'idle' && text(lane?.lane_id)) requestedGoals.add(text(lane.lane_id));
      break;
    }
  }

  return { questions, requestedMembers: [...requestedMembers], requestedGoals: [...requestedGoals] };
}

function questionActionKind(notification) {
  const question = notification?.question || {};
  if (question.response_mode === 'free_text') return 'resolved';
  const actions = Array.isArray(notification?.actions) ? notification.actions : [];
  const action = actions.find((candidate) => candidate && candidate.kind);
  if (action?.kind) return action.kind;
  return question.response_mode === 'ack' ? 'ack' : 'yes_no';
}

function recordAnswer(record) {
  return record?.answer || record?.envelope?.answer || record?.question?.answer || null;
}

function storedAnswerText(record, questionModel) {
  const answer = recordAnswer(record);
  if (!answer || typeof answer !== 'object') return '';
  if (typeof answer.text === 'string') return answer.text;
  if (typeof answer.custom_text === 'string') return answer.custom_text;
  const envelope = record?.envelope || record?.question?.envelope || {};
  let values = Array.isArray(answer.selections) ? answer.selections : [];
  if (!values.length && typeof answer.value?.answer === 'string') values = [answer.value.answer];
  if (!values.length && typeof answer.value === 'string') values = [answer.value];
  if (!values.length) return '';
  const options = Array.isArray(envelope.options) ? envelope.options : (questionModel?.options || []);
  const labels = values.map((value) => {
    const option = options.find((candidate) => String(candidate?.value) === String(value));
    return text(option?.label) || String(value);
  });
  return labels.join(', ');
}

function answerTimestamp(record) {
  return text(record?.answered_at || record?.envelope?.answered_at || record?.answer?.at || record?.envelope?.answer?.at);
}

function hasStoredAnswer(record) {
  const answer = recordAnswer(record);
  if (!answer || typeof answer !== 'object') return false;
  return (typeof answer.text === 'string' && !!answer.text.trim())
    || (typeof answer.custom_text === 'string' && !!answer.custom_text.trim())
    || (Array.isArray(answer.selections) && answer.selections.length > 0)
    || typeof answer.value?.answer === 'string'
    || typeof answer.value === 'string';
}

function latestAnsweredRecord(records) {
  return asArray(records)
    .filter((record) => ANSWERED_STATES.has(record?.state) && hasStoredAnswer(record))
    .sort((a, b) => {
      const left = Date.parse(answerTimestamp(a)); const right = Date.parse(answerTimestamp(b));
      if (Number.isFinite(left) && Number.isFinite(right)) return right - left;
      return answerTimestamp(b).localeCompare(answerTimestamp(a));
    })[0] || null;
}

function createQuestions(options = {}) {
  const doc = options.document || (typeof document !== 'undefined' ? document : null);
  const cc = options.cc || {};
  const laneReads = options.laneReads || null;
  const stateById = new Map();
  const answeredIds = new Set();
  const pageByScope = new Map();
  const mounts = new Map();
  const formsById = new Map();
  const saidBySpec = new Map();
  const saidLoads = new Map();
  const listeners = new Set();
  let data = { inventory: options.inventory || null, notifications: options.notifications || [], sessions: options.sessions || [] };
  const dataOverrides = new Set();
  let projection = { questions: [], requestedMembers: [], requestedGoals: [] };
  let unsubscribeReads = null;
  let disposed = false;

  function currentData() {
    if (!dataOverrides.has('inventory') && typeof options.getInventory === 'function') data.inventory = options.getInventory();
    if (!dataOverrides.has('notifications') && typeof options.getNotifications === 'function') data.notifications = options.getNotifications();
    if (!dataOverrides.has('sessions') && typeof options.getSessions === 'function') data.sessions = options.getSessions();
    return data;
  }

  function stateFor(id) {
    let current = stateById.get(id);
    if (!current) {
      current = { draft: { mode: null, values: [], text: '' }, phase: 'open', receiptText: '', retryMessage: '', timer: null, notification: null };
      stateById.set(id, current);
    }
    return current;
  }

  function releaseMountForms(mount) {
    for (const id of mount.formIds || []) {
      const forms = formsById.get(id);
      if (!forms) continue;
      for (const form of [...forms]) if (form === mount.formRefs?.get(id)) forms.delete(form);
      if (!forms.size) formsById.delete(id);
    }
    mount.formIds = new Set();
    mount.formRefs = new Map();
  }

  function setFormState(form, item) {
    const current = stateFor(item.id);
    const draft = current.draft;
    const selected = draft.mode === 'pick' ? new Set(draft.values.map(String)) : new Set();
    for (const button of form.querySelectorAll('[data-option-index]')) {
      const index = Number(button.dataset.optionIndex);
      const option = item.model.options?.find((candidate) => Number(candidate.index) === index);
      const on = !!option && selected.has(String(option.value));
      button.classList.toggle('is-selected', on);
      button.setAttribute('aria-checked', String(on));
    }
    const own = form.querySelector('.next-question-own');
    if (own) {
      if (own.value !== draft.text) own.value = draft.text;
      own.classList.toggle('is-active', draft.mode === 'text' && !!draft.text.trim());
      own.classList.toggle('is-dimmed', draft.mode === 'pick' && !!draft.text.trim());
    }
    const send = form.querySelector('.next-question-send');
    if (send) {
      send.disabled = !hasAnswer(item.model, draft) || ['sending', 'checking', 'sent', 'answered', 'closed', 'check_failed'].includes(current.phase);
    }
    form.querySelectorAll('button,textarea').forEach((control) => {
      if (control === send) return;
      control.disabled = ['sending', 'checking', 'sent', 'answered', 'closed', 'check_failed'].includes(current.phase);
    });
  }

  function syncForms(id) {
    for (const form of formsById.get(id) || []) {
      const item = form.__questionItem;
      if (item) setFormState(form, item);
    }
  }

  function registerForm(mount, form, item) {
    form.__questionItem = item;
    const forms = formsById.get(item.id) || new Set();
    forms.add(form);
    formsById.set(item.id, forms);
    mount.formIds.add(item.id);
    mount.formRefs.set(item.id, form);
  }

  function emit() {
    if (disposed) return;
    const snapshot = getSnapshot();
    if (typeof options.onChange === 'function') options.onChange(snapshot);
    for (const listener of listeners) listener(snapshot);
  }

  function requestRead(laneId, kind) {
    const method = kind === 'members' ? laneReads?.requestMembers : laneReads?.requestGoals;
    if (typeof method !== 'function') return;
    try { Promise.resolve(method.call(laneReads, laneId)).catch(() => {}); } catch (_) { /* the read controller exposes failure state */ }
  }

  function refresh() {
    if (disposed) return;
    projection = buildQuestionPlacement({ ...currentData(), laneReads });
    renderAll();
    for (const laneId of projection.requestedMembers) requestRead(laneId, 'members');
    for (const laneId of projection.requestedGoals) requestRead(laneId, 'goals');
    emit();
  }

  function visibleProjectionQuestions() {
    const now = Date.now();
    const all = projection.questions.map((item) => ({ ...item, state: stateFor(item.id) }));
    const byId = new Map(all.map((item) => [item.id, item]));
    for (const id of answeredIds) {
      const current = stateById.get(id);
      if (current?.phase !== 'sent' || current.receiptExpiresAt <= now) byId.delete(id);
    }
    const receipts = [];
    for (const [id, current] of stateById) {
      if (current.phase !== 'sent' || !current.notification || current.receiptExpiresAt <= now) continue;
      if (!byId.has(id)) {
        const source = projection.questions.find((item) => item.id === id);
        if (source) byId.set(id, { ...source, state: current });
        else {
          const placement = placementFor(current.notification, data.inventory?.lanes || [], laneReads, sessionIndex(data.sessions));
          byId.set(id, { id, notification: current.notification, model: durableQuestionOptionBModel(current.notification), title: questionTitle(current.notification), specId: questionSpecId(current.notification), producerId: questionProducerId(current.notification), placement, state: current });
        }
      }
      const item = byId.get(id);
      if (item) { item.state = current; item.receiptText = current.receiptText; receipts.push(id); }
    }
    const list = [...byId.values()];
    const openCount = projection.questions.reduce((count, item) => {
      const current = stateFor(item.id);
      return count + (answeredIds.has(item.id) || ['sent', 'answered', 'closed'].includes(current.phase) ? 0 : 1);
    }, 0);
    return { openCount, list, byLane: new Map(), byGoal: new Map(), bySpec: new Map(), youSaidBySpec: new Map(saidBySpec), _receipts: receipts };
  }

  function indexSnapshot(snapshot) {
    for (const item of snapshot.list) {
      const placement = item.placement;
      if (placement.kind !== 'lane' && placement.kind !== 'lane_goal') continue;
      appendMap(snapshot.byLane, placement.laneId, item);
      if (placement.kind === 'lane_goal') {
        let goals = snapshot.byGoal.get(placement.laneId);
        if (!goals) { goals = new Map(); snapshot.byGoal.set(placement.laneId, goals); }
        appendMap(goals, placement.goalId, item);
      }
      if (placement.specId) appendMap(snapshot.bySpec, placement.specId, item);
    }
    return snapshot;
  }

  function getSnapshot() {
    return indexSnapshot(visibleProjectionQuestions());
  }

  function appendMap(map, key, value) {
    if (!key) return;
    const list = map.get(key) || [];
    list.push(value);
    map.set(key, list);
  }

  function mountForm(parent, mount, item) {
    const current = stateFor(item.id);
    current.notification = item.notification;
    const model = item.model || {};
    const form = doc.createElement('div');
    form.className = 'next-question-form';
    form.dataset.questionId = item.id;
    const prompt = doc.createElement('p');
    prompt.className = 'next-question-prompt';
    prompt.textContent = text(model.prompt) || text(item.notification?.body);
    if (prompt.textContent) form.appendChild(prompt);

    if (model._unavailable) {
      parent.appendChild(form);
      return;
    }

    const options = Array.isArray(model.options) ? model.options.filter((option) => option && !option.meta) : [];
    if (options.length) {
      const group = doc.createElement('div');
      group.className = 'next-question-options';
      group.setAttribute('role', model.multiSelect ? 'group' : 'radiogroup');
      group.setAttribute('aria-label', 'Answers');
      for (const option of options) {
        const button = doc.createElement('button');
        button.type = 'button';
        button.className = 'next-question-option';
        button.dataset.optionIndex = String(option.index);
        button.setAttribute('role', model.multiSelect ? 'checkbox' : 'radio');
        button.setAttribute('aria-checked', 'false');
        const marker = doc.createElement('span');
        marker.className = 'next-question-mark';
        marker.setAttribute('aria-hidden', 'true');
        const label = doc.createElement('span');
        label.className = 'next-question-option-label';
        label.textContent = text(option.label) || String(option.value ?? '');
        button.append(marker, label);
        if (text(option.description)) {
          const description = doc.createElement('span');
          description.className = 'next-question-option-description';
          description.textContent = text(option.description);
          button.appendChild(description);
        }
        button.addEventListener('click', () => {
          const draft = stateFor(item.id).draft;
          const value = String(option.value ?? option.label ?? '');
          if (model.multiSelect) {
            const values = new Set(draft.values.map(String));
            if (values.has(value)) values.delete(value); else values.add(value);
            draft.values = [...values];
            draft.mode = draft.values.length ? 'pick' : null;
          } else if (draft.mode === 'pick' && draft.values.length === 1 && String(draft.values[0]) === value) {
            draft.values = [];
            draft.mode = null;
          } else {
            draft.values = [value];
            draft.mode = 'pick';
          }
          syncForms(item.id);
        });
        group.appendChild(button);
      }
      form.appendChild(group);
    }

    if (model.free_text === true) {
      const own = doc.createElement('textarea');
      own.className = 'next-question-own';
      own.rows = 2;
      own.placeholder = 'Or write your own answer';
      own.setAttribute('aria-label', 'Your own answer');
      own.addEventListener('input', () => {
        const draft = stateFor(item.id).draft;
        draft.text = own.value;
        if (draft.text.trim()) draft.mode = 'text';
        else if (draft.mode === 'text') draft.mode = null;
        syncForms(item.id);
      });
      own.addEventListener('focus', () => {
        const draft = stateFor(item.id).draft;
        if (draft.text.trim()) { draft.mode = 'text'; syncForms(item.id); }
      });
      own.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        void send(item);
      });
      form.appendChild(own);
    }

    const actions = doc.createElement('div');
    actions.className = 'next-question-actions';
    const sendButton = doc.createElement('button');
    sendButton.type = 'button';
    sendButton.className = 'next-question-send';
    sendButton.textContent = 'Send answer';
    sendButton.addEventListener('click', () => { void send(item); });
    actions.appendChild(sendButton);
    form.appendChild(actions);
    parent.appendChild(form);
    registerForm(mount, form, item);
    setFormState(form, item);
  }

  function hasAnswer(model, draft) {
    if (draft.mode === 'text') return !!draft.text.trim() && model.free_text === true;
    if (draft.mode === 'pick') return draft.values.length > 0;
    return false;
  }

  function answerPayload(item) {
    const draft = stateFor(item.id).draft;
    if (draft.mode === 'text' && draft.text.trim()) {
      return item.model.response_mode === 'free_text'
        ? { text: draft.text.trim(), submit: true }
        : { custom_text: draft.text.trim(), submit: true };
    }
    if (draft.mode === 'pick' && draft.values.length) return { selections: draft.values.map(String), submit: true };
    return null;
  }

  function answerLabel(item) {
    const draft = stateFor(item.id).draft;
    if (draft.mode === 'text') return draft.text.trim();
    if (draft.mode !== 'pick') return '';
    const labels = draft.values.map((value) => {
      const option = item.model.options?.find((candidate) => String(candidate.value) === String(value));
      return text(option?.label) || String(value);
    });
    return labels.join(', ');
  }

  function readArgs(item) {
    if (item.specId) return { spec_id: item.specId, open: false };
    if (item.producerId) return { producer_stream_id: item.producerId, open: false };
    return { open: false };
  }

  function findQuestionRecord(reply, id) {
    if (!Array.isArray(reply?.questions)) return null;
    return reply.questions.find((record) => text(record?.question_id) === id) || null;
  }

  function rememberYouSaid(specId, value) {
    if (!specId || !value?.text) return;
    const current = saidBySpec.get(specId);
    const previousAt = Date.parse(current?.answeredAt || '');
    const nextAt = Date.parse(value.answeredAt || '');
    if (current && Number.isFinite(previousAt) && Number.isFinite(nextAt) && previousAt > nextAt) return;
    saidBySpec.set(specId, value);
  }

  function markSent(item, answerText, record = null, promptConfirmed = false) {
    const current = stateFor(item.id);
    current.phase = 'sent';
    current.receiptText = answerText || storedAnswerText(record, item.model) || '';
    current.notification = item.notification;
    current.receiptExpiresAt = Date.now() + (Number.isFinite(options.receiptMs) ? options.receiptMs : RECEIPT_MS);
    answeredIds.add(item.id);
    if (current.timer) clearTimeout(current.timer);
    if (item.specId) {
      if (promptConfirmed && record && hasStoredAnswer(record)) {
        const entry = { text: storedAnswerText(record, item.model), answeredAt: answerTimestamp(record), record };
        rememberYouSaid(item.specId, entry);
        saidLoads.set(item.specId, { status: 'held', value: entry, promise: null });
      } else {
        // The normal success reply settles the receipt. You said is hydrated
        // from prompt.list when the spec is mounted, so it survives reloads.
        saidLoads.delete(item.specId);
      }
    }
    const snapshot = getSnapshot();
    for (const mount of mounts.values()) {
      if (mount.kind !== 'cards') continue;
      if (itemsForContext(snapshot, mount.context).some((candidate) => candidate.id === item.id)) {
        pageByScope.set(scopeKey(mount.context), item.id);
      }
    }
    current.timer = setTimeout(() => {
      if (current.phase !== 'sent') return;
      current.phase = 'answered';
      current.timer = null;
      renderAll();
      emit();
    }, Math.max(0, Number.isFinite(options.receiptMs) ? options.receiptMs : RECEIPT_MS));
    renderAll();
    emit();
  }

  async function reconcileAfterUncertain(item) {
    const current = stateFor(item.id);
    if (typeof cc.promptList !== 'function') {
      current.phase = 'check_failed';
      current.retryMessage = 'Answer status could not be checked.';
      renderAll(); emit();
      return;
    }
    current.phase = 'checking';
    renderAll(); emit();
    try {
      const reply = await cc.promptList(readArgs(item));
      if (!reply || reply.ok === false || !Array.isArray(reply.questions)) throw new Error(reply?.error || 'Question status could not be checked.');
      const record = findQuestionRecord(reply, item.id);
      if (record && hasStoredAnswer(record)) {
        markSent(item, storedAnswerText(record, item.model), record, true);
        return;
      }
      if (record?.state === 'open') {
        current.phase = 'not_sent';
        current.retryMessage = '';
        renderAll(); emit();
        return;
      }
      if (record) {
        current.phase = 'closed';
        answeredIds.add(item.id);
        renderAll(); emit();
        return;
      }
      throw new Error('Question status did not include this question.');
    } catch (_) {
      current.phase = 'check_failed';
      current.retryMessage = 'Answer status could not be checked.';
      renderAll(); emit();
    }
  }

  async function send(item) {
    const current = stateFor(item.id);
    if (['sending', 'checking', 'sent', 'answered', 'closed', 'check_failed'].includes(current.phase)) return;
    if (!hasAnswer(item.model, current.draft)) return;
    const notificationId = durableQuestionNotificationId(item.notification);
    if (!notificationId || typeof cc.notificationResolve !== 'function') {
      current.phase = 'not_sent';
      current.retryMessage = 'Answer sending is unavailable.';
      renderAll(); emit();
      return;
    }
    const payload = answerPayload(item);
    if (!payload) return;
    const receiptText = answerLabel(item);
    current.phase = 'sending';
    current.notification = item.notification;
    renderAll(); emit();
    try {
      const result = await cc.notificationResolve(notificationId, questionActionKind(item.notification), payload);
      if (!result || result.ok === false) throw new Error(result?.error || 'Question answer failed.');
      markSent(item, receiptText, result.question || result.notification?.question || null);
    } catch (_) {
      await reconcileAfterUncertain(item);
    }
  }

  function ensureYouSaid(specId) {
    const id = text(specId);
    if (!id || typeof cc.promptList !== 'function') return Promise.resolve(null);
    const existing = saidLoads.get(id);
    if (existing?.status === 'loading' || existing?.status === 'held' || existing?.status === 'failed') return existing.promise || Promise.resolve(existing.value || null);
    const load = { status: 'loading', promise: null, value: null };
    saidLoads.set(id, load);
    load.promise = Promise.resolve().then(() => cc.promptList({ spec_id: id, open: false })).then((reply) => {
      if (!reply || reply.ok === false || !Array.isArray(reply.questions)) throw new Error(reply?.error || 'Question status could not be checked.');
      const record = latestAnsweredRecord(reply.questions);
      const entry = record ? {
        text: storedAnswerText(record),
        answeredAt: answerTimestamp(record),
        record,
      } : null;
      load.status = 'held';
      load.value = entry;
      if (entry?.text) rememberYouSaid(id, entry);
      renderAll(); emit();
      return entry;
    }).catch(() => {
      load.status = 'failed';
      load.value = null;
      load.promise = null;
      renderAll(); emit();
      return null;
    });
    return load.promise;
  }

  function retryForQuestion(id) {
    const item = getSnapshot().list.find((candidate) => candidate.id === id);
    if (!item) return Promise.resolve(false);
    if (item.placement.retryIdentity && typeof options.retryQuestionIdentity === 'function') {
      return Promise.resolve(options.retryQuestionIdentity()).catch(() => false);
    }
    if (typeof laneReads?.retry !== 'function') return Promise.resolve(false);
    const reads = item.placement.retryReads || [];
    const tasks = reads.map(({ laneId, kind, goalId }) => {
      try { return Promise.resolve(laneReads.retry(laneId, kind, goalId)).catch(() => false); }
      catch (_) { return Promise.resolve(false); }
    });
    refresh();
    return Promise.all(tasks).then(() => { refresh(); return tasks.length > 0; });
  }

  function element(tag, className, parent, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    if (parent) parent.appendChild(node);
    return node;
  }

  function appendLaneIcon(parent, lane) {
    if (typeof options.renderLaneIcon === 'function') {
      try {
        const rendered = options.renderLaneIcon(lane, doc);
        if (rendered?.nodeType) { parent.appendChild(rendered); return; }
      } catch (_) { /* use the data-key fallback */ }
    }
    const icon = element('span', 'next-question-lane-icon', parent, '◈');
    icon.setAttribute('aria-hidden', 'true');
    if (lane?.icon) icon.dataset.icon = String(lane.icon);
  }

  function renderQuestionHeading(parent, item) {
    const placement = item.placement;
    if (placement.kind === 'lane' || placement.kind === 'lane_goal') {
      const link = element('button', 'next-question-heading-link', parent);
      link.type = 'button';
      link.dataset.testid = `next-question-heading-${item.id}`;
      const lane = (data.inventory?.lanes || []).find((candidate) => candidate.lane_id === placement.laneId)
        || { lane_id: placement.laneId, title: placement.laneTitle, icon: placement.laneIcon };
      appendLaneIcon(link, lane);
      element('strong', 'next-question-lane-title', link, placement.laneTitle);
      if (placement.kind === 'lane_goal' && placement.goalTitle) {
        element('span', 'next-question-heading-separator', link, '·');
        element('span', 'next-question-goal-title', link, placement.goalTitle);
      }
      link.addEventListener('click', () => options.onOpenSpec?.(placement.specId));
      return;
    }
    if (placement.kind === 'chat' && placement.chatId && placement.chatTitle) {
      const link = element('button', 'next-question-heading-link next-question-chat-link', parent, placement.chatTitle);
      link.type = 'button';
      link.dataset.testid = `next-question-heading-${item.id}`;
      link.addEventListener('click', () => options.onOpenChat?.(placement.chatId));
      return;
    }
    element('div', 'next-question-heading-title', parent, item.title);
  }

  function renderReceipt(parent, item, inCard) {
    const current = stateFor(item.id);
    if (current.phase === 'sent' && current.receiptExpiresAt > Date.now()) {
      const sent = element('div', 'next-question-sent', parent);
      element('span', 'next-question-sent-label', sent, 'Answer sent');
      element('span', 'next-question-sent-answer', sent, current.receiptText);
      return true;
    }
    if (current.phase === 'not_sent') element('div', 'next-question-not-sent', parent, 'Not sent');
    if (current.phase === 'check_failed') {
      const row = element('div', 'next-question-check-failed', parent);
      element('span', '', row, current.retryMessage || 'Answer status could not be checked.');
      const check = element('button', 'next-question-check', row, 'Check answer status');
      check.type = 'button';
      check.addEventListener('click', () => { void reconcileAfterUncertain(item); });
    }
    if (current.phase === 'sending' || current.phase === 'checking') {
      element('div', 'next-question-pending', parent, current.phase === 'checking' ? 'Checking answer status…' : 'Sending answer…');
    }
    return current.phase === 'sent' || current.phase === 'answered' || current.phase === 'closed' || (inCard && ['sending', 'checking'].includes(current.phase));
  }

  function renderListItem(parent, mount, item) {
    const row = element('article', 'next-question-list-item', parent);
    row.dataset.testid = `next-question-item-${item.id}`;
    row.dataset.questionId = item.id;
    const head = element('div', 'next-question-list-heading', row);
    renderQuestionHeading(head, item);
    const placement = item.placement;
    if (placement.kind === 'unplaced' && (placement.retryIdentity || placement.retryReads?.length)) {
      const retry = element('button', 'next-question-retry', head, 'Retry');
      retry.type = 'button';
      retry.addEventListener('click', () => { void retryForQuestion(item.id); });
    }
    const hasReceipt = renderReceipt(row, item, false);
    if (!hasReceipt && !['closed', 'answered'].includes(item.state.phase)) mountForm(row, mount, item);
  }

  function itemsForContext(snapshot, context) {
    const level = context?.level;
    let items = [];
    if (level === 'lane') items = snapshot.byLane.get(text(context.laneId)) || [];
    else if (level === 'goal') items = snapshot.byGoal.get(text(context.laneId))?.get(text(context.goalId)) || [];
    else if (level === 'spec') items = snapshot.bySpec.get(text(context.specId)) || [];
    if (context?.noGoal) items = items.filter((item) => item.placement.kind === 'lane');
    return items;
  }

  function scopeKey(context) {
    if (context?.level === 'lane') return `lane:${text(context.laneId)}${context.noGoal ? ':no-goal' : ''}`;
    if (context?.level === 'goal') return `goal:${text(context.laneId)}:${text(context.goalId)}`;
    return `spec:${text(context?.specId)}`;
  }

  function renderAbout(parent, item, context) {
    if (context.level === 'spec') return;
    const placement = item.placement;
    const about = placement.kind === 'lane_goal' && context.level === 'lane'
      ? [placement.goalTitle, placement.specTitle].filter(Boolean).join(' · ')
      : placement.specTitle;
    if (!about) return;
    const link = element('button', 'next-question-about', parent, about);
    link.type = 'button';
    link.addEventListener('click', () => options.onOpenSpec?.(placement.specId));
  }

  function renderCardItem(parent, mount, items, index, context) {
    const item = items[index];
    if (!item) return;
    const current = stateFor(item.id);
    const card = element('article', 'next-question-card', parent);
    card.classList.add(current.phase === 'sent' ? 'is-sent' : 'is-attention');
    card.dataset.testid = `next-question-card-${item.id}`;
    card.dataset.questionId = item.id;
    const header = element('header', 'next-question-card-header', card);
    const mark = element('span', 'next-question-card-mark', header, current.phase === 'sent' ? '✓' : '?');
    mark.setAttribute('aria-hidden', 'true');
    const label = element('span', 'next-question-card-label', header,
      current.phase === 'sent' ? 'Answer sent' : items.length > 1 ? `Question ${index + 1} of ${items.length}` : 'Needs your answer');
    label.dataset.testid = 'next-question-card-label';
    if (items.length > 1 && current.phase !== 'sent' && !['sending', 'checking', 'check_failed'].includes(current.phase)) {
      const spacer = element('span', 'next-question-card-spacer', header);
      spacer.setAttribute('aria-hidden', 'true');
      const previous = element('button', 'next-question-page next-question-prev', header, '‹');
      previous.type = 'button'; previous.setAttribute('aria-label', 'Previous question'); previous.dataset.testid = 'next-question-prev';
      previous.addEventListener('click', () => changePage(context, -1));
      const next = element('button', 'next-question-page next-question-next', header, '›');
      next.type = 'button'; next.setAttribute('aria-label', 'Next question'); next.dataset.testid = 'next-question-next';
      next.addEventListener('click', () => changePage(context, 1));
    }
    renderAbout(card, item, context);
    const receipt = renderReceipt(card, item, true);
    if (!receipt && !['closed', 'answered'].includes(current.phase)) mountForm(card, mount, item);
  }

  function changePage(context, delta) {
    const key = scopeKey(context);
    const snapshot = getSnapshot();
    const items = itemsForContext(snapshot, context);
    if (!items.length) return;
    const selectedId = pageByScope.get(key);
    const foundIndex = items.findIndex((item) => item.id === selectedId);
    const current = foundIndex < 0 ? 0 : foundIndex;
    const nextIndex = (current + delta + items.length) % items.length;
    pageByScope.set(key, items[nextIndex].id);
    for (const mount of mounts.values()) {
      if (mount.kind === 'cards' && mount.container && scopeKey(mount.context) === key) renderMount(mount);
    }
  }

  function renderSaid(container, specId) {
    const entry = saidBySpec.get(specId);
    container.replaceChildren();
    if (!entry?.text) return;
    const line = element('div', 'next-question-you-said', container);
    element('span', 'next-question-you-said-label', line, 'You said');
    element('span', 'next-question-you-said-answer', line, entry.text);
  }

  function renderMount(mount) {
    if (!doc || !mount.container || disposed) return;
    releaseMountForms(mount);
    if (mount.kind === 'said') {
      renderSaid(mount.container, mount.specId);
      return;
    }
    mount.container.replaceChildren();
    const snapshot = getSnapshot();
    if (mount.kind === 'list') {
      mount.container.classList.add('next-questions-list');
      if (!snapshot.list.length) {
        element('div', 'next-question-empty', mount.container, 'No questions waiting.');
        return;
      }
      for (const item of snapshot.list) renderListItem(mount.container, mount, item);
      return;
    }
    mount.container.classList.add('next-question-cards');
    if (mount.context?.level === 'spec') void ensureYouSaid(mount.context.specId);
    const items = itemsForContext(snapshot, mount.context);
    if (!items.length) return;
    const key = scopeKey(mount.context);
    const selectedId = pageByScope.get(key);
    let index = selectedId ? items.findIndex((item) => item.id === selectedId) : 0;
    if (index < 0) index = 0;
    pageByScope.set(key, items[index].id);
    renderCardItem(mount.container, mount, items, index, mount.context);
  }

  function renderAll() {
    for (const [container, mount] of [...mounts]) {
      if (container.isConnected === false && container.ownerDocument?.documentElement?.contains(container) === false) {
        releaseMountForms(mount);
        mounts.delete(container);
        continue;
      }
      renderMount(mount);
    }
  }

  function registerMount(container, mount) {
    if (!container || !doc) return () => {};
    const prior = mounts.get(container);
    if (prior) releaseMountForms(prior);
    mounts.set(container, mount);
    renderMount(mount);
    return () => {
      const current = mounts.get(container);
      if (current !== mount) return;
      releaseMountForms(mount);
      mounts.delete(container);
      container.replaceChildren();
    };
  }

  function mountList(container) {
    return registerMount(container, { kind: 'list', container, formIds: new Set(), formRefs: new Map() });
  }

  function mountCards(container, context = {}) {
    return registerMount(container, { kind: 'cards', container, context: { ...context }, formIds: new Set(), formRefs: new Map() });
  }

  function mountYouSaid(container, specId) {
    const id = text(specId);
    const unmount = registerMount(container, { kind: 'said', container, specId: id, formIds: new Set(), formRefs: new Map() });
    void ensureYouSaid(id);
    return unmount;
  }

  function update(next = {}) {
    if (Object.prototype.hasOwnProperty.call(next, 'inventory')) { data.inventory = next.inventory; dataOverrides.add('inventory'); }
    if (Object.prototype.hasOwnProperty.call(next, 'notifications')) { data.notifications = next.notifications; dataOverrides.add('notifications'); }
    if (Object.prototype.hasOwnProperty.call(next, 'sessions')) { data.sessions = next.sessions; dataOverrides.add('sessions'); }
    refresh();
    return getSnapshot();
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') return () => {};
    listeners.add(listener);
    listener(getSnapshot());
    return () => listeners.delete(listener);
  }

  function dispose() {
    disposed = true;
    unsubscribeReads?.();
    unsubscribeReads = null;
    for (const current of stateById.values()) if (current.timer) clearTimeout(current.timer);
    for (const [container, mount] of mounts) {
      releaseMountForms(mount);
      container.replaceChildren();
    }
    mounts.clear();
    listeners.clear();
  }

  if (typeof laneReads?.subscribe === 'function') {
    try { unsubscribeReads = laneReads.subscribe(() => refresh()); } catch (_) { unsubscribeReads = null; }
  }
  refresh();

  return { update, getSnapshot, mountList, mountCards, mountYouSaid, retryForQuestion, subscribe, dispose };
}

module.exports = {
  ANSWERED_STATES,
  RECEIPT_MS,
  buildQuestionPlacement,
  createQuestions,
  hasStoredAnswer,
  isQuestionOpen,
  latestAnsweredRecord,
  questionActionKind,
  questionId,
  storedAnswerText,
  uniqueOpenNotifications,
};
