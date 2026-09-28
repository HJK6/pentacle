// Opt-in embedded dashboard. The URL comes from the existing host config.
(function() {
'use strict';
const configuredUrl = window.HOST && window.HOST.dashboardHubConfig && window.HOST.dashboardHubConfig.scraperBotUrl;
if (typeof configuredUrl !== 'string' || !configuredUrl.trim()) return;
function mount(container) {
  const frame = document.createElement('iframe');
  frame.src = configuredUrl;
  frame.style.cssText = 'width:100%;height:100%;border:0;background:#0d1117;';
  frame.setAttribute('sandbox', 'allow-same-origin allow-scripts');
  container.replaceChildren(frame);
  return { frame };
}
window.DASHBOARDS.push({
  id: 'scraper-bot', name: 'Scraper Bot', description: 'Configured scraper dashboard',
  color: 'var(--blue)', mount, update() {}, unmount(refs) { refs.frame.remove(); },
  pollFn: async () => null, pollInterval: 60000,
});
})();
