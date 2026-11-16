'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { TODAY, item, event, emptyLists, fixtureSnapshot } = require('./fixtures/household_support');
const sel = () => require('../renderer/household/selectors');
const task = over => item({ id: 1, list: 'tasks', label: 'Synthetic task', ...over });
const snap = over => ({ ...fixtureSnapshot(), ...over });
test('mobile vectors: list order, names, placeholders and valid ids', () => {
  assert.deepEqual(sel().LIST_ORDER, ['tasks', 'grocery', 'meals', 'chores', 'study']);
  assert.deepEqual(Object.values(sel().LIST_META), [
    { name: 'To-do', placeholder: 'New task…' }, { name: 'Grocery', placeholder: 'Add an item…' },
    { name: 'Meals', placeholder: 'Add a meal…' }, { name: 'Chores', placeholder: 'Add a chore…' },
    { name: 'Study plan', placeholder: 'Add a study block…' }]);
  assert.equal(sel().isListId('tasks'), true); assert.equal(sel().isListId('custom'), false);
});
test('mobile vectors: priority/order/id sorting is stable and non-mutating', () => {
  const rows = [[1,'lo',1],[2,'med',5],[3,'hi',9],[4,'med',2],[5,'hi',3]].map(([id, priority, position]) => task({id,priority,position}));
  assert.deepEqual(sel().sortItems(rows).map(r=>r.id), [5,3,4,2,1]); assert.deepEqual(rows.map(r=>r.id),[1,2,3,4,5]);
  assert.deepEqual(sel().sortItems([9,7,8].map(id=>task({id,position:1}))).map(r=>r.id),[7,8,9]);
});
for (const [over,today,expected] of [
  [{due_date:'2054-10-05'},TODAY,[['OVERDUE','red']]], [{due_date:'2054-09-01',routine_id:7},TODAY,[['OVERDUE','red']]],
  [{due_date:TODAY},TODAY,[['DUE TODAY','red']]], [{due_date:'2054-10-07'},TODAY,[['DUE TOMORROW','amber']]],
  [{due_date:'2054-10-20'},TODAY,[['DUE OCT 20','amber']]], [{due_date:'2054-11-03'},TODAY,[['DUE NOV 3','amber']]],
  [{due_date:'2054-11-01'},'2054-10-31',[['DUE TOMORROW','amber']]], [{due_date:'2055-01-01'},'2054-12-31',[['DUE TOMORROW','amber']]],
  [{due_date:'2056-02-29'},'2056-02-28',[['DUE TOMORROW','amber']]], [{due_date:'2056-03-01'},'2056-02-28',[['DUE MAR 1','amber']]],
  [{priority:'hi'},TODAY,[['HIGH','amber']]], [{priority:'lo'},TODAY,[['LOW','muted']]], [{priority:'med'},TODAY,[]],
  [{due_date:TODAY,priority:'hi'},TODAY,[['DUE TODAY','red'],['HIGH','amber']]],
  [{due_date:'2054-10-20',priority:'lo'},TODAY,[['DUE OCT 20','amber'],['LOW','muted']]],
]) test(`mobile due/priority vector ${JSON.stringify(over)} from ${today}`, () => assert.deepEqual(sel().itemTags(task(over),today),expected.map(([text,tone])=>({text,tone}))));
test('mobile vectors: attribution only, never shared priority/due metadata', () => {
  for (const created_by of ['assistant','app','partner_assistant']) {
    const value={created_by,scope:'shared',priority:'hi',due_date:'2054-10-20'};
    assert.equal(sel().showItemLamp(task(value)), created_by==='assistant');
    assert.equal(sel().isAssistantEvent(event({id:1,date:TODAY,title:'Synthetic',...value})), created_by==='assistant');
  }
});
test('mobile vectors: critical rows preserve list/detail order and use daemon today', () => {
  assert.deepEqual(sel().criticalItems(fixtureSnapshot()).map(c=>[c.list,c.item.id]),[['tasks',2],['tasks',4],['tasks',1],['tasks',6],['grocery',10],['chores',20]]);
  const lists=emptyLists(); for(const [index,list] of ['study','chores','meals','grocery','tasks'].entries()) lists[list]=[item({id:90+index,list,label:'Synthetic',priority:'hi'})];
  assert.deepEqual(sel().criticalItems(snap({lists})).map(c=>c.list),['tasks','grocery','meals','chores','study']);
  lists.tasks=[task({id:1,due_date:'2054-11-01'}),task({id:2,due_date:'2054-11-02'})];
  assert.deepEqual(sel().criticalItems(snap({today:'2054-10-31',lists})).filter(c=>c.list==='tasks').map(c=>c.item.id),[1]);
});
test('mobile vectors: who, private suffix, partner name fallback', () => {
  for (const [who,label,bars] of [['self','ME',['me']],['partner','PARTNER FIXTURE',['partner']],['both','ME + PARTNER FIXTURE',['me','partner']]]) {
    for (const scope of ['private','shared']) assert.deepEqual(sel().whoDisplay(event({id:1,date:TODAY,title:'Synthetic',who,scope}),'Partner Fixture'), {label:label+(scope==='private'&&who!=='self'?' · PRIVATE':''),bars});
  }
  assert.equal(sel().partnerName(fixtureSnapshot()),'Partner Fixture');
  assert.equal(sel().partnerName(snap({people:undefined})),'Partner'); assert.equal(sel().partnerName(null),'Partner');
  assert.equal(sel().whoDisplay(event({id:1,date:TODAY,title:'Synthetic',who:'partner',scope:'shared'})).label,'PARTNER');
});
test('mobile vectors: today/all-day/time and upcoming four/7-day bounds', () => {
  assert.deepEqual(sel().todayEvents(fixtureSnapshot()).map(e=>e.id),[101,102,103,104]);
  assert.deepEqual(sel().upcomingEvents(fixtureSnapshot()).map(e=>e.id),[106,108,107,109]);
  const ev=(id,date,time)=>event({id,date,time,title:'Synthetic'});
  assert.deepEqual(sel().todayEvents(snap({events:[ev(1,TODAY,'14:30'),ev(2,TODAY,'09:05'),ev(3,TODAY,'00:00')]})).map(e=>e.id),[3,2,1]);
  assert.deepEqual(sel().upcomingEvents(snap({events:[ev(1,'2054-10-07','11:00'),ev(2,'2054-10-07',null)]})).map(e=>e.id),[2,1]);
  assert.deepEqual(sel().upcomingEvents(snap({today:'2054-10-28',events:[ev(1,'2054-11-04','10:00'),ev(2,'2054-11-05','10:00')]})).map(e=>e.id),[1]);
});
for(const [input,expected] of [['14:30','2:30p'],['09:05','9:05a'],['00:00','12:00a'],['12:00','12:00p'],['12:30','12:30p'],['01:00','1:00a'],['23:59','11:59p'],[null,'ALL DAY']]) test(`mobile formatTime ${input}`,()=>assert.equal(sel().formatTime(input),expected));
for(const [input,value] of [['',null],['4:30','04:30'],['04:30','04:30'],['16:30','16:30'],['0:00','00:00'],['23:59','23:59'],['4:30p','16:30'],['4:30 pm','16:30'],['9:05a','09:05'],['12:15am','00:15'],['12:15pm','12:15']]) test(`mobile parseTimeInput accepts ${input}`,()=>assert.deepEqual(sel().parseTimeInput(input),{ok:true,value}));
for(const input of ['25:00','4:3','abc','12:60','4','4:30x','13:00pm']) test(`mobile parseTimeInput rejects ${input}`,()=>assert.deepEqual(sel().parseTimeInput(input),{ok:false}));
for(const input of ['2054-10-06','2054-11-15','2056-02-29','2000-01-01','2100-12-31']) test(`mobile route date accepts ${input}`,()=>assert.equal(sel().parseRouteDate(input),input));
for(const input of [undefined,'','garbage','2054-13-01','2054-02-30','2055-02-29','2054-10-6','2054-10-06T00:00:00','10/06/2054','1999-12-31','2101-01-01','0000-01-01','9999-12-31']) test(`mobile route date rejects ${input}`,()=>assert.equal(sel().parseRouteDate(input),null));
test('mobile vectors: calendar math, labels, toggles and timezone independence', () => {
  const s=sel(); assert.equal(s.monthOf(TODAY),'2054-10'); assert.equal(s.monthOf('2100-12-31'),'2100-12');
  assert.equal(s.personalHeaderLabel(TODAY),'TUE · OCTOBER 6'); assert.equal(s.personalHeaderLabel('2054-10-31'),'SAT · OCTOBER 31'); assert.equal(s.personalHeaderLabel('2055-01-01'),'FRI · JANUARY 1');
  for(const [me,partner,who] of [[true,false,'self'],[false,true,'partner'],[true,true,'both']]) assert.equal(s.whoFromToggles({me,partner}),who);
  const run=()=>JSON.stringify({header:s.personalHeaderLabel(TODAY),tags:s.criticalItems(fixtureSnapshot()),today:s.todayEvents(fixtureSnapshot()),upcoming:s.upcomingEvents(fixtureSnapshot()),weekday:s.weekdayIndex(TODAY),days:s.daysInMonth('2056-02')});
  const baseline=run(); const methods=['getFullYear','getMonth','getDate','getDay','getHours','getMinutes','getSeconds','getTimezoneOffset','toDateString','toLocaleDateString','toLocaleString','toLocaleTimeString'];
  const saved=Object.fromEntries(methods.map(name=>[name,Date.prototype[name]]));
  try { for(const offset of [840,-720]) { for(const name of methods) Date.prototype[name]=()=>{throw Error(`Local date/zone used ${offset}`);}; assert.equal(run(),baseline); } }
  finally { for(const name of methods) Date.prototype[name]=saved[name]; }
});
test('mobile vectors: month paging helpers (shiftMonth, selectionForMonth, dateFieldLabel)', () => {
  const s=sel(); assert.equal(s.MIN_MONTH,'2000-01'); assert.equal(s.MAX_MONTH,'2100-12');
  for(const [month,n,expected] of [['2026-10',1,'2026-11'],['2026-12',1,'2027-01'],['2026-01',-1,'2025-12'],['2000-01',-1,'2000-01'],['2100-12',1,'2100-12']]) assert.equal(s.shiftMonth(month,n),expected);
  assert.equal(s.selectionForMonth('2026-10','2026-10-06'),'2026-10-06');
  assert.equal(s.selectionForMonth('2026-11','2026-10-06'),'2026-11-01');
  assert.equal(s.selectionForMonth('2025-10','2026-10-06'),'2025-10-01');
  assert.equal(s.dateFieldLabel('2026-10-06','2026-10-06'),'TUE OCT 6');
  assert.equal(s.dateFieldLabel('2026-11-20','2026-10-06'),'FRI NOV 20');
  assert.equal(s.dateFieldLabel('2027-01-05','2026-10-06'),'TUE JAN 5, 2027');
});
