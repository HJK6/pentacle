'use strict';

const fs = require('fs');
const path = require('path');

const out = path.join(__dirname, '..', 'test', 'e2e', 'fixtures', 'desktop_chat_reliability_perf_v1.jsonl');
const kinds = [
  ...Array(25).fill('USER'), ...Array(5).fill('USER_IMAGE'),
  ...Array(30).fill('ASSIST_MARKDOWN'), ...Array(10).fill('TOOL_USE'),
  ...Array(10).fill('TOOL_RESULT'), ...Array(5).fill('FILE_ACTION'),
  ...Array(5).fill('AGENT_MESSAGE'), ...Array(10).fill('DIVIDER'),
];
const rows = [];
for (let stream = 0; stream < 4; stream += 1) {
  for (let index = 0; index < 5000; index += 1) {
    const kind = kinds[index % kinds.length];
    const markdownIndex = (index % 100) - 30;
    const text = kind !== 'ASSIST_MARKDOWN' ? `${kind.toLowerCase()} row ${index + 1}`
      : markdownIndex < 5 ? `\`\`\`js\nconst row = ${index + 1};\n\`\`\``
        : markdownIndex < 10 ? `| row | value |\n|---|---|\n| ${index + 1} | perf |`
          : `markdown row ${index + 1}`;
    rows.push(JSON.stringify({
      schema_version: 1, stream_id: `perf-host:perf-${stream}`, daemon_seq: index + 1,
      timestamp: `2026-07-31T00:${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}.000Z`,
      kind, text,
    }));
  }
}
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, `${rows.join('\n')}\n`);
