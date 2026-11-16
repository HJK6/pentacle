import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { JSDOM } from 'jsdom';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderStreamTranscript } from '../renderer/src/shared_transcript_view';

test('guardian records stay out of rendered chat while identical genuine prose and permission questions remain', () => {
  // Drive the provider normalizer into the actual store and transcript DOM.
  const events = JSON.parse(execFileSync(process.env.V2_PYTHON_BIN || 'python3', ['-c', `
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path('services/chat-stream-v2').resolve()))
from codex_rollout_norm import normalize_codex_rollout_records
rows=[json.loads(line) for line in Path('services/chat-stream-v2/tests/fixtures/codex_guardian_review.jsonl').read_text().splitlines()]
primary=copy.deepcopy(rows)
primary[0]['payload']={'id':'primary-example','thread_source':'user','source':'cli'}
for r in primary[1:]:r['payload']['id']+='-genuine'
primary.append({'type':'response_item','timestamp':'2026-10-12T12:00:03Z','payload':{'type':'message','id':'approval-question','role':'assistant','content':[{'type':'output_text','text':'Allow the example command? Choose Allow or Deny.'}]}})
events=normalize_codex_rollout_records(rows+primary,host='example',session_name='v2-example')
for i,e in enumerate(events,1):e['daemon_seq']=i
print(json.dumps(events))
`], { encoding: 'utf8' }));
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', events: [], drafts: {}, sessions: [{
    stream_id: 'example:v2-example', host: 'example', provider: 'codex',
    session_name: 'v2-example', last_event_at: '', last_text: '', last_kind: '',
    draft: '', pending: false, working: false, online: true,
  }] });
  for (const event of events) store.applyFrame({ type: 'chat.event', event });
  // A replay of the same events must still render once.
  for (const event of events) store.applyFrame({ type: 'chat.event', event });
  const dom = new JSDOM('<!doctype html><div id="chat"></div>');
  try {
    const container = dom.window.document.getElementById('chat') as unknown as HTMLElement;
    renderStreamTranscript('example:v2-example', container, { store: store as never,
      chrome: { header: '#102a4a', accent: '#4da3ff', surface: '#0c1827', border: '#2f6ca5', title: 'Example' } });
    assert.equal(container.querySelectorAll('.slot-chat-row.is-user').length, 1);
    assert.equal(container.textContent?.split('risk_level').length, 2);
    assert.equal(container.textContent?.split('Synthetic internal authorization request.').length, 2);
    assert.ok(container.textContent?.includes('Allow the example command? Choose Allow or Deny.'));
    assert.equal(events.length, 3);
  } finally { dom.window.close(); }
});
