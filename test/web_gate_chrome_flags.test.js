'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { headlessChromeArgs } = require('./e2e/lib/chrome_launch.cjs');

// Keep strings (including template literals), remove only executable-source comments.
function stripComments(source) {
  return source.replace(/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\/\*[\s\S]*?\*\/|\/\/[^\n]*)/g,
    (all, literal) => literal || '');
}
const INVENTORY = {
  'compose_pills_proof.js': 1, 'grid_split_gate.js': 1, 'send_refusal_gate.cjs': 1, 'web_host_recovery_gate.cjs': 1,
  'own_assistant_bootstrap_browser.cjs': 1, 'web_assistant_direct_entry_gate.cjs': 2,
  'web_chat_history_paging_gate.cjs': 1, 'web_chat_history_retention_gate.cjs': 1,
  'web_chat_history_transport_gate.cjs': 1, 'web_default_pane_gate.cjs': 1,
  'web_gate.js': 1, 'web_popouts_gate.cjs': 1, 'web_sidebar_resize_gate.cjs': 1,
  'web_wrap_paste_gate.test.js': 1,
};
function audit(name, source, expected) {
  const code = stripComments(source);
  assert.doesNotMatch(code, /['"]--(?:headless(?:=new)?|use-mock-keychain)['"]/, `${name}: raw policy flags bypass helper`);
  assert.match(code, /require\(['"].*\/chrome_launch\.cjs['"]\)/, `${name}: helper import missing`);
  assert.equal((code.match(/(?:\.\.\.|args:\s*)headlessChromeArgs\(/g) || []).length, expected, `${name}: executable helper use missing`);
}

test('common Chrome args carry headless and keychain policy; Playwright owns its headless switch', () => {
  const supplied = ['--window-size=1600,900', 'about:blank'];
  const args = headlessChromeArgs(supplied);
  for (const flag of ['--headless=new','--use-mock-keychain','--no-first-run','--no-default-browser-check']) assert.ok(args.includes(flag));
  assert.deepEqual(args.slice(-2), supplied);
  assert.ok(!headlessChromeArgs([], { playwright: true }).some(a => a.startsWith('--headless')));
  assert.ok(headlessChromeArgs([], { playwright: true }).includes('--use-mock-keychain'));
});
test('every declared Chrome launcher uses the executable shared policy', () => {
  const dir = path.join(__dirname, 'e2e');
  for (const [file, count] of Object.entries(INVENTORY)) audit(file, fs.readFileSync(path.join(dir,file),'utf8'), count);
  audit('web_host_identity_browser.cjs', fs.readFileSync(path.join(__dirname,'web_host_identity_browser.cjs'),'utf8'), 1);
  function walk(folder) {
    for (const ent of fs.readdirSync(folder,{withFileTypes:true})) {
      if (['runs','node_modules'].includes(ent.name)) continue;
      const file = path.join(folder,ent.name);
      if (ent.isDirectory()) { walk(file); continue; }
      if (!/\.(js|cjs)$/.test(file) || file.endsWith('chrome_launch.cjs')) continue;
      const code = stripComments(fs.readFileSync(file,'utf8'));
      assert.doesNotMatch(code, /['"]--headless(?:=new)?['"]/, `${file}: unreviewed direct headless flags`);
      if (/chromium\.launch\(|spawn\((?:chrome\(|chrome\b|CHROME\b|chromeBin\b|browserBinary\b)/.test(code)) {
        assert.ok(Object.hasOwn(INVENTORY,path.relative(dir,file)), `${file}: add browser launcher to audited inventory`);
      }
    }
  }
  walk(dir);
});
test('audit mutations reject comment-only flags, missing helper use and old headless syntax', () => {
  const good = "const { headlessChromeArgs } = require('./lib/chrome_launch.cjs'); spawn(chrome, [...headlessChromeArgs()]);";
  audit('fixture',good,1);
  const missing = good.replace('...headlessChromeArgs()', '/* ...headlessChromeArgs(); --use-mock-keychain */');
  assert.notEqual(missing, good, 'mutation applied');
  assert.throws(() => audit('fixture',missing,1), /executable helper use missing/);
  assert.throws(() => audit('fixture',good.replace('...headlessChromeArgs()', "'--headless'"),1), /raw policy flags/);
  assert.throws(() => audit('fixture',good.replace('require(', '/* require(').replace("cjs');", "cjs'); */"),1), /helper import missing/);
});
