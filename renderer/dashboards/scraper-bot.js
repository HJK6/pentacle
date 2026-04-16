// ── Scraper Bot Dashboard (Mac mini only) ───────────────────────
// Embeds scraper-bot's live HTML dashboard (http://localhost:9020/)
// as a webview. Only registered when running directly on the mac-mini
// host — not from client machines, since scraper-bot binds to
// 127.0.0.1 on the mac-mini.

(function() {

const SCRAPER_BOT_URL = 'http://localhost:9020/';
const MAC_MINI_HOST_PREFIX = 'Bartimaeuss-Mac-mini';  // hostname starts with this

function mount(container) {
  container.innerHTML = `
    <iframe
      src="${SCRAPER_BOT_URL}"
      style="width:100%;height:100%;border:0;background:#0d1117;"
      sandbox="allow-same-origin allow-scripts"
    ></iframe>`;
  return {};
}

function update(refs, data) { /* dashboard refreshes itself via meta refresh every 5s */ }
function unmount(refs) { /* iframe torn down when container.innerHTML is replaced */ }

// Synchronous host gate — only register on the mac-mini host.
const _host = (window.HOST && window.HOST.hostname) || '';
const _isClient = !!(window.HOST && window.HOST.isClient);
if (!_isClient && _host.startsWith(MAC_MINI_HOST_PREFIX)) {
  window.DASHBOARDS.push({
    id: 'scraper-bot',
    name: 'Scraper Bot',
    description: 'Live Chrome sessions managed by scraper-bot (localhost:9020)',
    color: 'var(--blue)',
    mount, update, unmount,
    // No-op pollFn — the embedded dashboard auto-refreshes every 5s itself
    // via its own meta refresh. app.js still wants a pollFn to schedule.
    pollFn: async () => null,
    pollInterval: 60_000,  // minimal churn; data is null-ignored by update()
  });
}

})();
