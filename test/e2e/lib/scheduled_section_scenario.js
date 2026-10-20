'use strict';

// Web "scheduled-section" scenario (spec_pentacle__recurring_schedules_app_2026_10).
//
// The sidebar Scheduled section against the seeded gate daemon (seed_web_gate.py
// SCHEDULE_FIXTURE: one active weekly rule in America/Chicago and one pending
// one-shot, both days from firing). Pause, Resume and Edit go through the
// served page's window.cc; each is read back from the daemon with
// schedule.recurring.get, not from the page.
const RULE_ID = '5ced0000-0000-4000-8000-000000000001';
const ONE_SHOT_ID = 'sched-web-gate-oneshot';

async function scheduledSection(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  if (!fixture) { report.note('no fixture: skipping the scheduled-section scenario (observational run)'); return; }
  const { waitForValue } = require('./web_scenarios');
  const rule = `document.querySelector('#scheduled-section [data-recurring-id="${RULE_ID}"]')`;
  const daemonRule = () => session.eval(`window.cc.recurringGet(${JSON.stringify(RULE_ID)}).then((r) => r.recurring)`);
  const waitDaemon = (predicate, label) => waitForValue(
    session, cdp, `window.cc.recurringGet(${JSON.stringify(RULE_ID)}).then((r) => r.recurring)`, predicate,
    { timeoutMs, label },
  );
  const click = (action) => session.eval(`(() => { const b = ${rule}.querySelector('[data-scheduled-action="${action}"]'); b.click(); return true; })()`);

  const rows = await waitForValue(session, cdp, `(() => {
    const section = document.getElementById('scheduled-section');
    const row = ${rule};
    return { visible: !!section && section.style.display !== 'none' && section.offsetParent !== null,
      rule: row ? row.textContent : '', oneShot: !!document.querySelector('[data-schedule-id="${ONE_SHOT_ID}"]'),
      oneShotText: (document.querySelector('[data-schedule-id="${ONE_SHOT_ID}"]') || {}).textContent || '',
      order: !!row && !!document.querySelector('[data-schedule-id="${ONE_SHOT_ID}"]')
        && !!(row.compareDocumentPosition(document.querySelector('[data-schedule-id="${ONE_SHOT_ID}"]')) & Node.DOCUMENT_POSITION_FOLLOWING) };
  })()`, (v) => v.visible && v.rule && v.oneShot, { timeoutMs, label: 'Scheduled section with the seeded rule and one-shot' });
  report.ok('the Scheduled section lists the recurring rule, then the pending one-shot', rows.order, rows);
  report.ok('the rule renders its recurrence in the rule zone', /weekly \w+ at 02:00 America\/Chicago/.test(rows.rule), { rule: rows.rule });
  report.ok('the rule shows its next run in the zone with relative time', /Next: .*(CDT|CST).*\(in /.test(rows.rule), { rule: rows.rule });
  report.ok('the pending one-shot shows its prompt preview', rows.oneShotText.includes('web gate one-shot reminder'), rows);
  report.ok('one-shot rows carry no actions', await session.eval(
    `document.querySelectorAll('[data-schedule-id="${ONE_SHOT_ID}"] [data-scheduled-action]').length === 0`));

  const before = await daemonRule();
  report.ok('the daemon row starts active', before.state === 'active', { state: before.state });

  await click('pause');
  const paused = await waitDaemon((v) => v && v.state === 'paused', 'daemon row paused after Pause');
  report.ok('Pause pauses the daemon row', paused.state === 'paused');
  await waitForValue(session, cdp, `!!${rule} && ${rule}.dataset.state === 'paused' && !!${rule}.querySelector('[data-scheduled-action="resume"]')`,
    (v) => v === true, { timeoutMs, label: 'paused badge and Resume button' });

  await click('resume');
  const resumed = await waitDaemon((v) => v && v.state === 'active', 'daemon row active after Resume');
  report.ok('Resume reactivates the daemon row', resumed.state === 'active');
  await waitForValue(session, cdp, `!!${rule} && ${rule}.dataset.state === 'active'`, (v) => v === true,
    { timeoutMs, label: 'active badge after Resume' });

  await click('edit');
  await waitForValue(session, cdp, `!!${rule} && !!${rule}.querySelector('form.scheduled-edit')`, (v) => v === true,
    { timeoutMs, label: 'Edit form' });
  await session.eval(`(() => {
    const input = ${rule}.querySelector('input[name="at_time"]');
    input.value = '03:30'; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await click('save');
  const edited = await waitDaemon((v) => v && v.recurrence && v.recurrence.at_time === '03:30', 'daemon at_time 03:30 after Save');
  report.ok('Edit changes at_time on the daemon row', edited.recurrence.at_time === '03:30', { recurrence: edited.recurrence });
  report.ok('Edit re-arms the next fire at the new time', edited.next_fire_at_utc !== before.next_fire_at_utc
    && /T0[89]:30:00/.test(edited.next_fire_at_utc), { before: before.next_fire_at_utc, after: edited.next_fire_at_utc });
  await waitForValue(session, cdp, `!!${rule} && !${rule}.querySelector('form.scheduled-edit') && ${rule}.textContent.includes('03:30')`,
    (v) => v === true, { timeoutMs, label: 'section shows the new time and closes the form' });
}

module.exports = { scheduledSection };
