// Walk: question_clear_unlock
// Lane B chat_streamd reliability: question clear/unlock behavior.
//
// Drives a throwaway Claude session into AskUserQuestion, answers the rendered
// question, and asserts current client behavior: the question slot clears and
// the composer unlocks. question_nonce consumption is intentionally out of
// scope for this walk.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['claude'], providers: ['claude'] };

const QUESTION_PROMPT = [
  'You MUST use the AskUserQuestion tool right now (do NOT answer in plain text).',
  'Ask me exactly one question: "Which lane should continue?"',
  'Use exactly three options: Alpha, Beta, Gamma.',
  'After I choose, output exactly one sentence containing the option I selected.',
].join('\n');

async function sendPrompt(ctx, cell, before, text) {
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 20000, label: 'composer send enabled' },
  );
  await ctx.type(`${cell} .slot-chat-compose-input`, text);
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000,
    label: 'optimistic insert',
  });
}

async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, 'claude');
  const cell = `#cell-${slot}`;
  const sid = JSON.stringify(streamId);
  await sendPrompt(ctx, cell, ctx.beaconSeq(), QUESTION_PROMPT);

  const parsed = await ctx.waitFor(
    `(() => {
      const q = window.PentacleChatStore.getQuestion(${sid});
      if (!q || !Array.isArray(q.options) || q.options.filter(o => !o.meta).length < 3) return false;
      return JSON.stringify({ header: q.header || '', prompt: q.prompt || '', labels: q.options.filter(o => !o.meta).map(o => o.label), noncePresent: !!q.question_nonce });
    })()`,
    { timeoutMs: 150000, label: 'question appears in store' },
  );
  const pd = JSON.parse(parsed);
  ctx.log('question payload: ' + parsed);
  ctx.assert('question UI payload appeared with options', pd.labels.length >= 3, pd);

  await ctx.waitFor(`document.querySelectorAll('${cell} .slot-chat-question-option[data-option]').length >= 3`, {
    timeoutMs: 15000,
    label: 'question option buttons rendered',
  });
  await ctx.screenshot('question-visible');

  const selected = await ctx.eval(`(() => {
    const btns = [...document.querySelectorAll('${cell} .slot-chat-question-option[data-option]')];
    const target = btns.find(b => /Beta/i.test(b.textContent || '')) || btns[1] || btns[0];
    if (!target) return null;
    return { option: target.getAttribute('data-option'), text: target.textContent || '' };
  })()`);
  ctx.assert('answer option selected for click', selected && selected.option, selected);
  ctx.assert('answer option clicked', await ctx.click(`${cell} .slot-chat-question-option[data-option="${selected.option}"]`), selected);

  await ctx.waitFor(`window.PentacleChatStore.getQuestion(${sid}) === null`, {
    timeoutMs: 60000,
    label: 'question slot cleared after answer',
  });
  ctx.assert('question slot cleared after answer', true);

  const unlocked = await ctx.waitFor(
    `(() => {
      const input = document.querySelector('${cell} .slot-chat-compose-input');
      const send = document.querySelector('${cell} .slot-chat-compose-send');
      const question = document.querySelector('${cell} .slot-chat-question');
      const hidden = !question || question.style.display === 'none' || question.children.length === 0;
      const phase = window.PentacleChatStore.getTurnPhase(${sid});
      if (hidden && input && !input.disabled && send && !send.disabled && phase === 'idle') {
        return JSON.stringify({ hidden, inputDisabled: input.disabled, sendDisabled: send.disabled, phase });
      }
      return false;
    })()`,
    { timeoutMs: 120000, label: 'question cleared and composer unlocked' },
  );
  ctx.assert('input unlocked after question answer', JSON.parse(unlocked).phase === 'idle', JSON.parse(unlocked));
  await ctx.screenshot('question-cleared-unlocked');
}

module.exports = { SCENARIO_META, run };

