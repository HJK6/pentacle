'use strict';

function durableQuestionNotificationId(notification) {
  return String(notification?.notification_id || notification?.id || '').trim();
}

function mergeDurableQuestionNotification(previous, notification) {
  const merged = { ...previous, ...notification, question: { ...previous?.question, ...notification?.question } };
  if (merged.producer !== 'agent_question.v1' || !String(merged.answer_to_stream_id || merged.question?.producer_stream_id || '').trim()) return null;
  const open = value => String(value?.state || 'open') === 'open' && String(value?.question?.state || 'open') === 'open';
  if (previous && !open(previous) && open(merged)) return null;
  return merged;
}


function durableQuestionOptionBModel(notification) {
  const question = notification?.question || {};
  let options = Array.isArray(question.options) ? question.options : [];
  if (!options.length && question.response_mode === 'ack') {
    options = [{ label: 'Acknowledge', value: 'ack' }];
  }
  const allowCustom = question.allow_custom === true || question.allowCustom === true;
  return {
    ...question,
    free_text: question.response_mode === 'free_text' || allowCustom,
    _unavailable: Array.isArray(question.questions) && question.questions.length > 0,
    question_key: question.question_id || durableQuestionNotificationId(notification),
    header: notification?.title || 'Question',
    prompt: notification?.body || '',
    multiSelect: question.response_mode === 'multi_choice',
    customText: allowCustom && question.response_mode !== 'free_text',
    options: options.map((opt, index) => ({
      index: index + 1,
      label: opt?.label || String(opt?.value ?? `Option ${index + 1}`),
      value: Object.prototype.hasOwnProperty.call(opt || {}, 'value')
        ? opt.value
        : (opt?.label || String(index + 1)),
      description: typeof opt?.description === 'string' ? opt.description : undefined,
    })),
  };
}


module.exports = { durableQuestionNotificationId, durableQuestionOptionBModel, mergeDurableQuestionNotification };
