'use strict';

function questionItems(question) {
  if (question && Array.isArray(question.questions) && question.questions.length) {
    return question.questions.map((item, pos) => ({
      ...item,
      _draftIndex: Number.isFinite(Number(item.index)) ? Number(item.index) : pos,
      _displayIndex: pos + 1,
      multiSelect: item.multiSelect ?? (question.questions.length === 1 ? question.multiSelect : undefined),
    }));
  }
  return [{
    ...question,
    index: 0,
    _draftIndex: 0,
    _displayIndex: 1,
    header: question?.header,
    prompt: question?.prompt || '',
    options: Array.isArray(question?.options) ? question.options : [],
    multiSelect: !!question?.multiSelect,
    customText: !!question?.customText,
    free_text: question?.free_text !== false,
  }];
}

function selectableOptions(item) {
  return (Array.isArray(item.options) ? item.options : [])
    .filter((opt) => !opt?.meta)
    .filter((opt) => Number.isFinite(Number(opt.index)));
}

function optionValueForIndex(item, index) {
  const match = selectableOptions(item).find((opt) => Number(opt.index) === Number(index));
  if (!match) return String(index);
  if (Object.prototype.hasOwnProperty.call(match, 'value')) return match.value;
  return match.label || String(index);
}

function promptText(item, fallbackQuestion) {
  const header = String(item.header || '').trim();
  const prompt = String(item.prompt || fallbackQuestion?.prompt || '').trim();
  return header ? `${header}: ${prompt}`.trim().replace(/:\s*$/, '') : prompt;
}

function ensureDraft(drafts, streamId, questionSig) {
  if (!streamId) return { sig: questionSig, answers: {} };
  if (!drafts[streamId] || drafts[streamId].sig !== questionSig) {
    drafts[streamId] = { sig: questionSig, answers: {} };
  }
  return drafts[streamId];
}

function answerHasContent(answer) {
  if (!answer) return false;
  if (Number.isFinite(Number(answer.optionIndex)) && Number(answer.optionIndex) > 0) return true;
  if (Array.isArray(answer.optionIndices) && answer.optionIndices.length > 0) return true;
  if (typeof answer.text === 'string' && answer.text.trim()) return true;
  return false;
}

function answerHasSelection(answer) {
  if (!answer) return false;
  if (Number.isFinite(Number(answer.optionIndex)) && Number(answer.optionIndex) > 0) return true;
  if (Array.isArray(answer.optionIndices) && answer.optionIndices.length > 0) return true;
  return false;
}

function buildAnswersForQuestion(question, draft) {
  return questionItems(question).map((item) => {
    const raw = draft.answers[item._draftKey ?? item._draftIndex] || {};
    const answer = {};
    if (item.multiSelect) {
      const selected = Array.isArray(raw.optionIndices)
        ? raw.optionIndices.map((value) => Number(value)).filter((value) => Number.isFinite(value) && value > 0)
        : [];
      if (selected.length) {
        answer.selectedOptionIndices = selected;
        answer.selectedOptionValues = selected.map((index) => optionValueForIndex(item, index));
      }
    } else if (Number.isFinite(Number(raw.optionIndex)) && Number(raw.optionIndex) > 0) {
      const selected = Number(raw.optionIndex);
      answer.selectedOptionIndex = selected;
      answer.selectedOptionValue = optionValueForIndex(item, selected);
      answer.selectedOptionValues = [answer.selectedOptionValue];
    }
    if (typeof raw.text === 'string' && raw.text.trim()) {
      if (item.customText) answer.customText = raw.text;
      else answer.text = raw.text;
    }
    if (typeof raw.note === 'string' && raw.note.trim()) answer.note = raw.note;
    return answer;
  });
}

function answerConstraint(item, answer, { submitRequiresSelection = false } = {}) {
  if (item._locked) return '';
  if (item._unavailable) return 'Question unavailable';
  if (!submitRequiresSelection && item.free_text !== false && answer?.text?.trim()) return '';
  const selected = item.multiSelect ? answer?.optionIndices || [] : answer?.optionIndex ? [answer.optionIndex] : [];
  if (!selected.length || selected.some(index => !selectableOptions(item).some(opt => Number(opt.index) === Number(index)))) return 'Choose an answer';
  const number = (...values) => values.filter(value => value !== undefined && value !== null && value !== '')
    .map(Number).find(value => Number.isFinite(value) && value >= 0);
  if (item.multiSelect) {
    const min = number(item.min_select, item.min_selected, item.minSelections, item.min);
    const max = number(item.max_select, item.max_selected, item.maxSelections, item.max);
    if (min !== undefined && selected.length < min) return `Select at least ${min} options`;
    if (max !== undefined && selected.length > max) return `Select at most ${max} options`;
  }
  return '';
}

function allAnswersValid(question, draft, options = {}) {
  const items = questionItems(question);
  return items.length > 0 && items.every(item => !answerConstraint(item, draft.answers[item._draftKey ?? item._draftIndex], options));
}

function setAllControlsDisabled(container, disabled) {
  container.querySelectorAll('button, input, textarea').forEach((el) => {
    el.disabled = disabled;
  });
}

function renderQuestionOptionB(opts) {
  const {
    container,
    doc = (typeof document !== 'undefined' ? document : null),
    question,
    streamId,
    questionSig,
    alreadyAnswered = false,
    drafts = {},
    answeredSig = {},
    buildAnswerText,
    onSubmit,
    onCancel,
    allowFreeText = true,
    submitRequiresSelection = false,
    showCancel = true,
    pagerLabel = null,
    canPagePrev = false,
    canPageNext = false,
    onPagePrev,
    onPageNext,
    visibleItemIndex,
    onDraftChange,
  } = opts || {};
  if (!container || !doc || !question) return;
  if (typeof buildAnswerText !== 'function') {
    throw new Error('buildAnswerText is required for question Option B rendering');
  }

  const draft = ensureDraft(drafts, streamId, questionSig);
  const allItems = questionItems(question);
  for (const item of allItems) {
    const key = item._draftKey ?? item._draftIndex;
    if (item._signature && draft.answers[key]?._signature !== item._signature) draft.answers[key] = { _signature: item._signature };
  }
  const items = Number.isInteger(visibleItemIndex) ? allItems.slice(visibleItemIndex, visibleItemIndex + 1) : allItems;
  const valid = () => allAnswersValid(question, draft, { submitRequiresSelection });
  const fields = doc.createElement('div');
  fields.className = 'slot-chat-question-fields';
  container.appendChild(fields);
  const stackEl = items.length > 1 ? doc.createElement('div') : fields;
  if (items.length > 1) stackEl.className = 'slot-chat-question-stack';

  const refreshSubmit = () => {
    const submit = container.querySelector('.slot-chat-question-submit');
    if (submit) submit.disabled = alreadyAnswered || !valid();
    const error = container.querySelector('.slot-chat-question-constraint');
    if (error) error.textContent = items.map(item => answerConstraint(item, draft.answers[item._draftKey ?? item._draftIndex], { submitRequiresSelection })).filter(text => text && text !== 'Choose an answer').join(' · ');
    onDraftChange?.(draft);
  };
  const refreshNote = (card, answer) => {
    const note = card.querySelector('.slot-chat-question-note');
    if (!note) return;
    note.hidden = !answerHasSelection(answer);
  };

  for (const item of items) {
    const card = items.length > 1 ? doc.createElement('div') : fields;
    if (items.length > 1) {
      card.className = 'slot-chat-question-card';
      card.dataset.questionIndex = String(item._draftIndex);
    }
    const current = draft.answers[item._draftKey ?? item._draftIndex] || {};
    draft.answers[item._draftKey ?? item._draftIndex] = current;

    const promptEl = doc.createElement('div');
    promptEl.className = 'slot-chat-question-prompt';
    promptEl.textContent = promptText(item, question) || `Question ${item._displayIndex}`;
    card.appendChild(promptEl);

    const optionsEl = doc.createElement('div');
    optionsEl.className = `slot-chat-question-options${item.multiSelect ? ' is-multiselect' : ''}`;
    for (const opt of selectableOptions(item)) {
      const index = Number(opt.index);
      const btn = doc.createElement('button');
      btn.type = 'button';
      btn.className = `slot-chat-question-option${item.multiSelect ? ' is-checkbox' : ''}`;
      btn.dataset.option = String(index);
      btn.dataset.questionIndex = String(item._draftIndex);
      if (item.multiSelect) btn.setAttribute('role', 'checkbox');
      const isSelected = item.multiSelect
        ? (Array.isArray(current.optionIndices) && current.optionIndices.includes(index))
        : Number(current.optionIndex) === index;
      if (isSelected) btn.classList.add('is-selected');
      if (item.multiSelect) btn.setAttribute('aria-checked', isSelected ? 'true' : 'false');
      btn.disabled = alreadyAnswered || !!item._locked || !!item._unavailable;
      const labelEl = doc.createElement('span');
      labelEl.className = 'slot-chat-question-option-label';
      labelEl.textContent = opt.label || String(index);
      btn.appendChild(labelEl);
      if (opt.description) {
        const descEl = doc.createElement('span');
        descEl.className = 'slot-chat-question-option-desc';
        descEl.textContent = opt.description;
        btn.appendChild(descEl);
      }
      btn.addEventListener('click', () => {
        current.text = '';
        current.customActive = false;
        card.querySelector('.slot-chat-question-custom')?.classList.remove('is-selected');
        const free = card.querySelector('.slot-chat-question-freetext');
        if (free) { free.value = ''; if (item.customText) free.hidden = true; }
        if (item.multiSelect) {
          if (!Array.isArray(current.optionIndices)) current.optionIndices = [];
          const at = current.optionIndices.indexOf(index);
          if (at >= 0) current.optionIndices.splice(at, 1);
          else current.optionIndices.push(index);
          current.optionIndices.sort((a, b) => a - b);
          const selected = current.optionIndices.includes(index);
          btn.classList.toggle('is-selected', selected);
          btn.setAttribute('aria-checked', selected ? 'true' : 'false');
        } else {
          current.optionIndex = index;
          card.querySelectorAll('.slot-chat-question-option').forEach((other) => {
            other.classList.toggle('is-selected', other === btn);
          });
        }
        refreshNote(card, current);
        refreshSubmit();
      });
      optionsEl.appendChild(btn);
    }
    card.appendChild(optionsEl);

    if (allowFreeText && item.free_text !== false) {
      const freeText = doc.createElement('textarea');
      freeText.className = 'slot-chat-question-freetext';
      freeText.dataset.questionIndex = String(item._draftIndex);
      freeText.dataset.questionKey = String(item._draftKey || '');
      freeText.rows = 2;
      freeText.placeholder = 'Type an answer';
      if (item.customText) freeText.placeholder = 'Type a custom answer';
      freeText.disabled = alreadyAnswered || !!item._locked || !!item._unavailable;
      freeText.value = current.text || '';
      freeText.hidden = !!item.customText && !current.customActive && !current.text;
      freeText.addEventListener('input', () => {
        current.text = freeText.value;
        {
          current.customActive = !!item.customText;
          freeText.hidden = false;
          delete current.note;
          const note = card.querySelector('.slot-chat-question-note');
          if (note) note.value = '';
          card.querySelector('.slot-chat-question-custom')?.classList.add('is-selected');
          delete current.optionIndex;
          current.optionIndices = [];
          card.querySelectorAll('.slot-chat-question-option').forEach((btn) => {
            btn.classList.remove('is-selected');
            if (btn.classList.contains('is-checkbox')) btn.setAttribute('aria-checked', 'false');
          });
        }
        refreshNote(card, current);
        refreshSubmit();
      });
      if (item.customText && selectableOptions(item).length) {
        const custom = doc.createElement('button');
        custom.type = 'button';
        custom.className = `slot-chat-question-custom${current.customActive || current.text ? ' is-selected' : ''}`;
        custom.textContent = 'Custom answer';
        custom.disabled = freeText.disabled;
        custom.addEventListener('click', () => {
          current.customActive = true;
          freeText.hidden = false;
          freeText.dispatchEvent(new doc.defaultView.Event('input'));
          freeText.focus();
        });
        card.appendChild(custom);
      }
      card.appendChild(freeText);
    }

    const note = doc.createElement('textarea');
    note.className = 'slot-chat-question-note';
    note.dataset.questionIndex = String(item._draftIndex);
    note.dataset.questionKey = String(item._draftKey || '');
    note.rows = 2;
    note.placeholder = 'Add a note';
    note.disabled = alreadyAnswered || !!item._locked || !!item._unavailable;
    note.value = current.note || '';
    note.hidden = !answerHasSelection(current);
    note.addEventListener('input', () => {
      current.note = note.value;
      refreshSubmit();
    });
    card.appendChild(note);

    if (items.length > 1) stackEl.appendChild(card);
  }
  if (items.length > 1) fields.appendChild(stackEl);

  const constraint = doc.createElement('div');
  constraint.className = 'slot-chat-question-constraint';
  constraint.setAttribute('role', 'status');
  container.appendChild(constraint);
  const actionsEl = doc.createElement('div');
  actionsEl.className = 'slot-chat-question-actions';
  if (pagerLabel) {
    const pager = doc.createElement('div');
    pager.className = 'slot-chat-question-pager';
    const prev = doc.createElement('button');
    prev.type = 'button';
    prev.className = 'slot-chat-question-page';
    prev.textContent = 'Prev';
    prev.disabled = !canPagePrev;
    prev.addEventListener('click', () => {
      if (!prev.disabled && typeof onPagePrev === 'function') onPagePrev();
    });
    const label = doc.createElement('span');
    label.className = 'slot-chat-question-page-label';
    label.textContent = pagerLabel;
    const next = doc.createElement('button');
    next.type = 'button';
    next.className = 'slot-chat-question-page';
    next.textContent = 'Next';
    next.disabled = !canPageNext;
    next.addEventListener('click', () => {
      if (!next.disabled && typeof onPageNext === 'function') onPageNext();
    });
    pager.appendChild(prev);
    pager.appendChild(label);
    pager.appendChild(next);
    actionsEl.appendChild(pager);
  }
  const submitBtn = doc.createElement('button');
  submitBtn.type = 'button';
  submitBtn.className = 'slot-chat-question-submit';
  submitBtn.textContent = 'Send answers';
  submitBtn.disabled = alreadyAnswered || !valid();
  const cancelBtn = doc.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'slot-chat-question-cancel';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.disabled = alreadyAnswered;
  actionsEl.appendChild(submitBtn);
  if (showCancel) actionsEl.appendChild(cancelBtn);
  container.appendChild(actionsEl);
  refreshSubmit();

  submitBtn.addEventListener('click', async () => {
    if (alreadyAnswered || !valid()) return;
    const answers = buildAnswersForQuestion(question, draft);
    const text = buildAnswerText({ question, answers });
    if (streamId) answeredSig[streamId] = questionSig;
    setAllControlsDisabled(container, true);
    try {
      if (typeof onSubmit === 'function') await onSubmit(text, { question, answers });
    } catch (error) {
      if (streamId && answeredSig[streamId] === questionSig) delete answeredSig[streamId];
      setAllControlsDisabled(container, false);
      refreshSubmit();
    }
  });

  if (showCancel) {
    cancelBtn.addEventListener('click', async () => {
      if (streamId) answeredSig[streamId] = questionSig;
      setAllControlsDisabled(container, true);
      try {
        if (typeof onCancel === 'function') await onCancel();
      } catch (error) {
        if (streamId && answeredSig[streamId] === questionSig) delete answeredSig[streamId];
        setAllControlsDisabled(container, false);
        refreshSubmit();
      }
    });
  }
}

module.exports = {
  answerConstraint,
  allAnswersValid,
  answerHasContent,
  answerHasSelection,
  buildAnswersForQuestion,
  questionItems,
  renderQuestionOptionB,
};
