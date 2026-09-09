// Walk: working_no_tool_fallback
// public-ui-regression - Bug 2.
//
// Drives a real claude turn that runs a Bash tool. While the stream is still
// non-idle and the session summary reports a tool/bash last event, the default
// desktop transcript selector and the visible chat DOM must not surface a tool
// fallback row. After the turn settles, the final assistant bubble must render.
const { spawnThrowawayChat } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc'], requires: ['claude'], providers: ['claude'] };

const FINAL_TEXT = 'FINAL-NO-TOOL-FALLBACK';
const COMMAND = 'sleep 8 && echo TOOL-FALLBACK-OK';
const PROMPT = [
  'Use the Bash tool to run EXACTLY this command and nothing else first:',
  COMMAND,
  `Do not reply until it finishes; then reply with exactly: ${FINAL_TEXT}`,
].join('\n');

function cleanTranscriptProbe({ sid, cell }) {
  return `(() => {
    const detail = window.PentacleChatStore.selectSessionDetail(${sid});
    const items = (detail && detail.transcriptItems) || [];
    const toolItems = items.filter((item) => {
      const rule = String(item.displayRule || '');
      return item.tone === 'tool' ||
        item.tone === 'thinking' ||
        rule === 'activity:command' ||
        rule === 'activity:tool-output' ||
        rule === 'activity:tool-batch' ||
        rule === 'activity:collapsed-tool' ||
        rule === 'activity:explored' ||
        rule === 'activity:file-change' ||
        rule === 'activity:code-block';
    }).map((item) => ({ tone: item.tone, displayRule: item.displayRule, text: item.text }));
    const root = document.querySelector('${cell}');
    const domToolNodes = root ? [...root.querySelectorAll('.slot-chat-activity, .slot-chat-command-card, .slot-chat-file-card')] : [];
    const assistantCards = root ? [...root.querySelectorAll('.slot-chat-assistant-card')] : [];
    return {
      itemCount: items.length,
      toolItems,
      domToolRows: domToolNodes.length,
      commandTextVisibleInToolRows: domToolNodes.some((node) => {
        const text = node.textContent || '';
        return text.includes(${JSON.stringify(COMMAND)}) || text.includes('TOOL-FALLBACK-OK');
      }),
      finalVisibleInAssistant: assistantCards.some((node) => (node.textContent || '').includes(${JSON.stringify(FINAL_TEXT)})),
    };
  })()`;
}

async function run(ctx) {
  const { slot, streamId } = await spawnThrowawayChat(ctx, 'claude');
  const cell = `#cell-${slot}`;
  const sid = JSON.stringify(streamId);

  const before = ctx.beaconSeq();
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 15000, label: 'composer send enabled' },
  );
  ctx.assert('composer accepted Bash prompt input', await ctx.type(`${cell} .slot-chat-compose-input`, PROMPT), { slot });
  await ctx.waitFor(
    `(() => { const el = document.querySelector('${cell} .slot-chat-compose-input'); return el && el.value.includes(${JSON.stringify(COMMAND)}); })()`,
    { timeoutMs: 5000, label: 'composer input contains Bash prompt' },
  );
  await ctx.waitFor(
    `(() => { const b = document.querySelector('${cell} .slot-chat-compose-send'); return b && !b.disabled; })()`,
    { timeoutMs: 5000, label: 'composer send enabled after prompt input' },
  );
  ctx.assert('send button clicked for Bash prompt', await ctx.click(`${cell} .slot-chat-compose-send`), { slot });
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000,
    label: 'optimistic insert',
  });

  const liveToolSummary = await ctx.waitFor(`(() => {
    const state = window.PentacleChatStore.getState();
    const session = (state.sessions || []).find((s) => s.stream_id === ${sid});
    const phase = window.PentacleChatStore.getTurnPhase(${sid});
    const kind = String((session && session.last_kind) || '');
    const text = String((session && session.last_text) || '');
    const toolLike = /^TOOL/i.test(kind) || /Bash|Command running|TOOL-FALLBACK-OK|${COMMAND.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/i.test(text);
    if (phase === 'idle' || !toolLike) return false;
    return { phase, kind, lastText: text.slice(0, 240) };
  })()`, { timeoutMs: 45000, label: 'turn working with tool/bash session summary' });
  ctx.assert('turn is non-idle while session summary is tool/bash', liveToolSummary && liveToolSummary.phase !== 'idle', liveToolSummary);

  const cleanWhileWorking = await ctx.eval(cleanTranscriptProbe({ sid, cell }));
  ctx.assert('working clean selector has no tool-action/tool-tone fallback row',
    cleanWhileWorking.toolItems.length === 0, cleanWhileWorking);
  ctx.assert('working chat DOM has no visible tool/command activity row',
    cleanWhileWorking.domToolRows === 0 && cleanWhileWorking.commandTextVisibleInToolRows === false, cleanWhileWorking);
  await ctx.screenshot('working-no-tool-fallback');

  await ctx.waitFor(`window.PentacleChatStore.getTurnPhase(${sid}) === 'idle'`, {
    timeoutMs: 90000,
    label: 'turn settled to idle',
  });
  const final = await ctx.waitFor(`(() => {
    const detail = window.PentacleChatStore.selectSessionDetail(${sid});
    const items = (detail && detail.transcriptItems) || [];
    const assistant = items.filter((item) => item.tone === 'assistant' || item.displayRule === 'bubble:assistant');
    const root = document.querySelector('${cell}');
    const assistantCards = root ? [...root.querySelectorAll('.slot-chat-assistant-card')] : [];
    const assistantDomHasFinal = assistantCards.some((node) => (node.textContent || '').includes(${JSON.stringify(FINAL_TEXT)}));
    return assistant.some((item) => String(item.text || '').includes(${JSON.stringify(FINAL_TEXT)})) && assistantDomHasFinal
      ? { assistantCount: assistant.length, domHasFinal: true }
      : false;
  })()`, { timeoutMs: 30000, label: 'final assistant bubble rendered' });
  ctx.assert('final assistant bubble rendered after idle', final && final.domHasFinal === true, final);
  await ctx.screenshot('idle-final-assistant');
}

module.exports = { SCENARIO_META, run };

