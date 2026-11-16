// Select the configured test tuple through the actual New Chat controls.
async function selectTestPromptProfile(ctx, provider) {
  const result = await ctx.eval(`(async () => {
    const response = await window.cc.chatSpawnCatalog();
    const tuple = response?.catalog?.profiles?.test_prompt?.[${JSON.stringify(provider)}];
    if (!response?.ok || !Array.isArray(tuple) || tuple.length !== 2) return false;
    const values = [['#spawn-provider', ${JSON.stringify(provider)}],
                    ['#spawn-model', tuple[0]], ['#spawn-effort', tuple[1]]];
    for (const [selector, value] of values) {
      const select = document.querySelector(selector);
      if (!select || ![...select.options].some(option => option.value === value)) return false;
      select.value = value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return values.every(([selector, value]) => document.querySelector(selector)?.value === value);
  })()`);
  ctx.assert('configured test tuple selected before prompt', result);
  if (!result) throw new Error('Configured test tuple unavailable in real picker');
}
module.exports = { selectTestPromptProfile };
