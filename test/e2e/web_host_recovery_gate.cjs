// Actual DOM -> /cc proxy -> web host -> main client -> daemon -> counted native provider.
// Counters persist outside both restarted processes. --quick is development only.
"use strict";
const fs = require("fs"), path = require("path"), assert = require("node:assert/strict"), crypto = require("crypto"), http = require("http");
const { spawn, spawnSync, execFileSync } = require("child_process"), { once } = require("events");
const ROOT = process.env.PHASE2_ROOT || path.resolve(__dirname, "../..");
const requireRoot = require("module").createRequire(ROOT + "/package.json");
const WS = requireRoot("ws");
const cdp = require("./lib/cdp"), { headlessChromeArgs } = require("./lib/chrome_launch.cjs"), { getFreePort } = require("./lib/disposable_web_daemon_config");
const out = path.resolve(process.argv[2]), python = process.argv[3], red = process.argv.includes("--red"), kind = process.env.PHASE2_KIND || "ordinary", beforeAdmission = process.argv.includes("--before-admission"), manualOverlap = process.argv.includes("--manual-overlap"), daemonGap = process.argv.includes("--daemon-gap");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms)), hash = (b) => crypto.createHash("sha256").update(b).digest("hex"), write = (n, v) => fs.writeFileSync(out + "/" + n, JSON.stringify(v, null, 2));
async function wait(read, test, label, ms = 2e4) {
  let v;
  const end = Date.now() + ms;
  while (Date.now() < end) {
    v = await read();
    if (test(v)) return v;
    await sleep(50);
  }
  throw Error(label + ": " + JSON.stringify(v));
}
async function stop(p) {
  if (!p || p.exitCode !== null || p.signalCode) return;
  p.kill("SIGTERM");
  await Promise.race([once(p, "exit"), sleep(4e3)]);
  if (p.exitCode === null && !p.signalCode) {
    p.kill("SIGKILL");
    await once(p, "exit");
  }
}
const quote = (s) => "'" + s.replaceAll("'", "'\\''") + "'";
async function main() {
  fs.mkdirSync(out, { recursive: true, mode: 448 });
  assert.equal(fs.readdirSync(out).length, 0);
  let daemon, web, browser, page, dproxy, hproxy, wproxy;
  const sockets = /* @__PURE__ */ new Set(), trace = [], wire = [], rpc = [], webPids = [], daemonPids = [];
  let tmuxStarted = false;
  const ns = "ptest-web-recovery-" + crypto.randomBytes(5).toString("hex"), tmux = execFileSync("which", ["tmux"], { encoding: "utf8" }).trim(), tmuxBin = out + "/tmux";
  fs.writeFileSync(tmuxBin, "#!/bin/sh\nexec " + quote(tmux) + " -L " + quote(ns) + ' -f /dev/null "$@"\n', { mode: 448 });
  const env = { PATH: process.env.PATH, LANG: "en_US.UTF-8", SHELL: "/bin/sh", HOME: out + "/home", USERPROFILE: out + "/home", PENTACLE_RUNTIME_DIR: out + "/runtime", PENTACLE_CONFIG: out + "/client.cjs", PENTACLE_INGEST_INTERVAL_S: ".1", PENTACLE_ADMIT_DURABLE: "1" };
  fs.mkdirSync(env.HOME, { recursive: true });
  const submissions = () => fs.existsSync(out + "/submissions.jsonl") ? fs.readFileSync(out + "/submissions.jsonl", "utf8").trim().split("\n").filter(Boolean).map(JSON.parse) : [];
  const wrapper = ROOT + "/test/e2e/lib/web_gate_daemon.py", fixture = process.env.PHASE2_FIXTURE || ROOT + "/test/e2e/lib/web_host_recovery_fixture.py";
  let daemonPort, webPort;
  let blocked = false, lose = true, lost = null, acceptedAt = null, allowProof = false, connections = 0, baseline = 0;
  const text = "phase2 " + kind + " lost ACK";
  const stream = kind === "composite" ? "fixture:assistant" : "local:send-refusal-fixture";
  const startDaemon = async () => {
    const f = fs.openSync(out + "/daemon.log", "a");
    daemon = spawn(python, [wrapper, out, "--host", "127.0.0.1", "--port", String(daemonPort), "--local-host", "local", "--db", out + "/sessions.db", "--notifications-db", out + "/notifications.db", "--assets-db", out + "/assets.db", "--blob-root", out + "/blobs", "--tmux-bin", tmuxBin, "--projects-root", out, "--disable-hosts", "--disable-mirror", "--disable-nudges", "--disable-outbound-notices", "--disable-remote-presence", "--disable-reconciler"], { cwd: ROOT, env, stdio: ["ignore", f, f] });
    fs.closeSync(f);
    daemonPids.push(daemon.pid);
    await wait(async () => {
      try {
        const s = new WS("ws://127.0.0.1:" + daemonPort);
        await once(s, "open");
        s.close();
        return true;
      } catch {
        return false;
      }
    }, Boolean, "daemon listening");
    trace.push({ event: "daemon_started", pid: daemon.pid, at: Date.now() });
  };
  const read = (name = "api/health") => new Promise((resolve, reject) => {
    http.get("http://127.0.0.1:" + webPort + "/" + name, (r) => {
      const c = [];
      r.on("data", (b) => c.push(b));
      r.on("end", () => r.statusCode === 200 ? resolve(Buffer.concat(c)) : reject(Error("HTTP" + r.statusCode)));
    }).on("error", reject);
  });
  const startWeb = async () => {
    const f = fs.openSync(out + "/web.log", "a");
    web = spawn(process.execPath, [ROOT + "/server", "--profile", out + "/client.cjs", "--bind", "127.0.0.1", "--port", String(webPort)], { cwd: ROOT, env, stdio: ["ignore", f, f] });
    fs.closeSync(f);
    webPids.push(web.pid);
    await wait(async () => {
      try {
        await read();
        return true;
      } catch {
        return false;
      }
    }, Boolean, "web listening");
    trace.push({ event: "web_started", pid: web.pid, at: Date.now() });
  };
  const affected = (o) => lost && o && typeof o === "object" && (o.request_id === lost.requestId || o.optimistic_id === lost.optimisticId || o.text === text || o.message === text);
  const scrub = (o) => {
    if (!o || typeof o !== "object" || allowProof) return o;
    if (affected(o)) return void 0;
    if (Array.isArray(o)) return o.map(scrub).filter((v) => v !== void 0);
    return Object.fromEntries(Object.entries(o).map(([k, v]) => [k, scrub(v)]));
  };
  const dom = () => page.eval(`({rows:[...document.querySelectorAll('#cell-0 .slot-chat-row.is-user')].map(e=>e.textContent),optimistic:window.PentacleChatStore.getState().optimisticSends,connected:window.PentacleChatStore.getState().connected,body:document.querySelector('#cell-0')?.innerText})`);
  let classification = "HARNESS_ERROR";
  try {
    write("source.json", { head: execFileSync("git", ["rev-parse", "HEAD"], { cwd: ROOT, encoding: "utf8" }).trim(), files: Object.fromEntries(["renderer/web_cc.js", "renderer/app.js", "renderer/src/chat_store_controller.ts", "renderer/src/chat_core_entry.ts", "main/chat_stream_client.js", "main/cc_handlers.js", "test/e2e/web_host_recovery_gate.cjs", "test/e2e/lib/web_host_recovery_fixture.py", "test/e2e/lib/web_gate_daemon.py", "renderer/dist/web/bundle.js", "renderer/dist/web/chat_core.bundle.js"].map((n) => [n, hash(fs.readFileSync(ROOT + "/" + n))])), harness_sha256: hash(fs.readFileSync(__filename)), kind, red });
    fs.writeFileSync(out + "/native.jsonl", "");
    execFileSync(python, [wrapper, out, "--issue"], { cwd: ROOT, env });
    execFileSync(tmuxBin, ["new-session", "-d", "-s", "send-refusal-fixture", "-x", "180", "-y", "50", [python, fixture, "provider", out].map(quote).join(" ")], { env });
    tmuxStarted = true;
    await wait(() => execFileSync(tmuxBin, ["capture-pane", "-p", "-t", "=send-refusal-fixture:"], { encoding: "utf8" }), (v) => v.includes("READY"), "provider ready");
    execFileSync(python, [fixture, "seed", out], { cwd: ROOT, env });
    if (kind === "composite") {
      const b = JSON.parse(fs.readFileSync(out + "/backend.json"));
      Object.assign(env, { PENTACLE_ASSISTANT_COMPOSITE_ENABLED: "1", PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID: stream, PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID: "local:send-refusal-fixture", PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION: b.generation, PENTACLE_ASSISTANT_MIRROR_ENABLED: "0" });
    }
    daemonPort = await getFreePort();
    webPort = await getFreePort();
    await startDaemon();
    dproxy = new WS.Server({ host: "127.0.0.1", port: 0 });
    await once(dproxy, "listening");
    dproxy.on("connection", (down) => {
      const up = new WS("ws://127.0.0.1:" + daemonPort), q = [];
      for (const s of [up, down]) {
        sockets.add(s);
        s.on("error", () => {
        });
      }
      up.on("open", () => q.splice(0).forEach((b) => up.send(b)));
      down.on("message", (raw) => {
        const f = JSON.parse(raw);
        if (f.type === "send") wire.push({ frame: f, sha256: hash(raw), at: Date.now() });
        up.readyState === 1 ? up.send(raw) : q.push(raw);
      });
      up.on("message", (raw) => {
        const f = JSON.parse(raw);
        if (["send.result", "send.error", "send.indeterminate"].includes(f.type)) {
          trace.push({ event: "daemon_frame", frame: f, at: Date.now() });
          if (lost && f.request_id === lost.requestId && f.type === "send.result") acceptedAt ??= Date.now();
        }
        if (down.readyState === 1) down.send(raw);
      });
      up.on("close", (code, reason) => {
        trace.push({event:'daemon_proxy_closed',code,at:Date.now()});
        down.close([1005,1006].includes(code) ? 1000 : code, reason);
      });
      down.on("close", () => up.terminate());
    });
    fs.writeFileSync(out + "/client.cjs", "module.exports=" + JSON.stringify({ features: { chatUi: true, inputBar: true, mic: false, dashboards: false }, hosts: { local: { kind: "local" } }, agents: {}, tmux: tmuxBin, chatStream: { url: "ws://127.0.0.1:" + dproxy.address().port, localHost: "local", hosts: ["local"], tokenPath: out + "/operator-auth/token", autoStart: false } }));
    await startWeb();
    hproxy = http.createServer((req, res) => {
      const up = http.request({ host: "127.0.0.1", port: webPort, path: req.url, method: req.method, headers: { ...req.headers, host: "127.0.0.1:" + webPort, ...req.headers.origin ? { origin: "http://127.0.0.1:" + webPort } : {} } }, (r) => {
        res.writeHead(r.statusCode, r.headers);
        r.pipe(res);
      });
      up.on("error", () => {
        res.writeHead(503).end();
      });
      req.pipe(up);
    });
    wproxy = new WS.Server({ noServer: true });
    hproxy.on("upgrade", (req, sock, head) => wproxy.handleUpgrade(req, sock, head, (down) => wproxy.emit("connection", down, req)));
    await new Promise((r) => hproxy.listen(0, "127.0.0.1", r));
    wproxy.on("connection", (down, req) => {
      connections++;
      if (blocked) {
        down.close();
        return;
      }
      const up = new WS("ws://127.0.0.1:" + webPort + "/cc", { headers: { origin: "http://127.0.0.1:" + webPort, host: "127.0.0.1:" + webPort } }), q = [], stateIds = new Set();
      for (const s of [up, down]) {
        sockets.add(s);
        s.on("error", () => {
        });
      }
      up.on("open", () => q.splice(0).forEach((b) => up.send(b)));
      down.on("message", (raw) => {
        const f = JSON.parse(raw);
        if (f.method === "chat-stream:get-state") stateIds.add(f.id);
        if (f.method === "chat-stream:send" && f.args[2] === text) {
          rpc.push({ frame: f, at: Date.now(), sha256: hash(JSON.stringify(f.args)) });
          if (!lost) {
            lost = { requestId: f.args[3], optimisticId: f.args[4], rpcId: f.id };
            if (beforeAdmission) {
              trace.push({ event: "request_dropped_before_host", at: Date.now() });
              lose = false;
              down.close();
              return;
            }
          } else {
            allowProof = true;
            trace.push({ event: f.args[3] === lost.requestId ? "recovery_rpc" : "new_intent_rpc", request_id: f.args[3], at: Date.now() });
            if (manualOverlap && rpc.length === 2) {
              allowProof = false;
              setTimeout(() => { if (up.readyState === 1) up.send(raw); }, 1200);
              return;
            }
          }
        }
        up.readyState === 1 ? up.send(raw) : q.push(raw);
      });
      up.on("message", (raw) => {
        const f = JSON.parse(raw);
        if (stateIds.has(f.id)) trace.push({event:'resync_snapshot',connected:f.result?.connected,at:Date.now()});
        if (lose && lost && f.id === lost.rpcId && f.result?.request_id === lost.requestId) {
          trace.push({ event: "browser_ACK_lost", frame: f, at: Date.now() });
          lose = false;
          blocked = !red;
          setTimeout(() => down.close(), 0);
          return;
        }
        const v = scrub(f);
        if (v && down.readyState === 1) down.send(JSON.stringify(v));
      });
      up.on("close", () => down.close());
      down.on("close", () => up.terminate());
    });
    const debug = await getFreePort();
    browser = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [...headlessChromeArgs(), "--disable-gpu", "--window-size=1600,1000", "--user-data-dir=" + out + "/chrome", "--remote-debugging-port=" + debug, "http://127.0.0.1:" + hproxy.address().port], { stdio: "ignore" });
    page = await cdp.connect(debug, { timeoutMs: 2e4 });
    await page.waitFor("window.PentacleChatStore?.getState().connected === true", { timeoutMs: 2e4 });
    await page.waitFor(`window.PentacleChatStore.getState().sessions.some(s=>s.stream_id===${JSON.stringify(stream)})`, { timeoutMs: 2e4 });
    assert.equal(await page.eval(`window.focusStreamId(${JSON.stringify(stream)})`), true);
    await page.click("#header-0 [data-mode=chat]");
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-compose-input')", { timeoutMs: 1e4 });
    const urgentExists = await page.eval(`!!document.querySelector('#cell-0 [aria-label="Send urgently"]')`);
    write("urgent-path-observation.json", { exists: urgentExists, expected: false, actualDOM: true });
    assert.equal(urgentExists, false, 'operator urgency control is retired');
    const assets = await page.eval(`(async()=>{const o={};for(const f of ['bundle.js','chat_core.bundle.js']){const r=await fetch(f),h=await crypto.subtle.digest('SHA-256',await r.arrayBuffer());o[f]=Array.from(new Uint8Array(h)).map(x=>x.toString(16).padStart(2,'0')).join('');}return o})()`);
    for (const [n, h] of Object.entries(assets)) assert.equal(h, hash(fs.readFileSync(ROOT + "/renderer/dist/web/" + n)));
    write("served-assets.json", assets);
    classification = "PRODUCT_FAIL";
    if (kind === "urgent") {
      await page.type("#cell-0 .slot-chat-compose-input", "prime the working urgent target");
      await page.waitFor("document.querySelector('#cell-0 .slot-chat-compose-send')?.disabled === false");
      await page.click("#cell-0 .slot-chat-compose-send");
      await wait(submissions, (v) => v.length === 1, "working-target distinct input");
      const primeId = wire.find(w => w.frame.text === 'prime the working urgent target').frame.request_id;
      await wait(() => trace.some(t => t.event === 'daemon_frame' && t.frame.type === 'send.result'
        && t.frame.request_id === primeId && t.frame.delivery === 'landed'), Boolean, 'prime receipt completed');
      await sleep(50);
      await wait(() => page.eval(`window.PentacleChatStore.getTurnPhase(${JSON.stringify(stream)})`), (v) => v !== "idle", "target working");
      baseline = 1;
    }
    await page.type("#cell-0 .slot-chat-compose-input", text);
    await page.waitFor("document.querySelector('#cell-0 .slot-chat-compose-send')?.disabled === false");
    await page.click("#cell-0 .slot-chat-compose-send");
    if (beforeAdmission) {
      await wait(() => rpc.length, (n) => n === 2, "one automatic recovery after no host admission");
      await wait(submissions, (v) => v.length === baseline + 1, "one first physical submission");
      await wait(dom, (d2) => d2.rows.filter((t) => t.includes(text)).length === 1 && !Object.values(d2.optimistic || {}).some((o) => o.request_id === lost.requestId && o.status === "indeterminate"), "one reconciled row");
      const attempted = wire.filter(w => w.frame.request_id === lost.requestId);
      const refused = trace.filter(t => t.event === 'daemon_frame' && t.frame.request_id === lost.requestId
        && t.frame.type === 'send.error' && t.frame.error_code === 'server_overloaded' && t.frame.retryable === true);
      assert(attempted.length >= 1 && attempted.length <= 2);
      if (attempted.length === 2) assert.equal(refused.length, 1, 'only the proven phase1 pre-admission retry permits another main wire frame');
      assert.equal(new Set(attempted.map(w => w.sha256)).size, 1);
      assert.equal(new Set(rpc.map((r) => r.sha256)).size, 1);
      write("verdict.json", { status: "PASS", cell: "R1", kind, lost, submissions: submissions(), rpc, wire, trace, dom: await dom(), initialHostAdmission: 0, physicalOriginalSubmissions: 1 });
      return;
    }
    await wait(() => trace.some((t) => t.event === "browser_ACK_lost"), Boolean, "ACK lost");
    await wait(submissions, (v) => v.length === baseline + 1, "one provider submission");
    if (red) {
      await wait(dom, (d3) => Object.values(d3.optimistic || {}).some((o) => o.request_id === lost.requestId && o.status === "failed"), "failed row on real /cc loss");
      await wait(() => connections, (n) => n > 1, "actual /cc reopened");
      await sleep(500);
      const d2 = await dom();
      assert.equal(rpc.length, 1);
      assert.equal(submissions().length, 1);
      await page.screenshot(out + "/red.png");
      write("verdict.json", { status: "EXPECTED_RED", urgentMissing: true, transport: "actual /cc closed and reopened after ACK loss", connections, rpc, wire, trace, dom: d2, submissions: submissions() });
      return;
    }
    await wait(dom, (d2) => Object.values(d2.optimistic || {}).some((o) => o.request_id === lost.requestId && o.status === "indeterminate"), "uncertain after real /cc loss");
    write("delay-checkpoint.json", { acceptedAt, lost, dom: await dom(), submissions: submissions() });
    console.log("R3_DELAY_STARTED " + JSON.stringify({ kind, acceptedAt }));
    if (daemonGap) {
      await stop(daemon);
      const gapAt = Date.now();
      blocked = false;
      await wait(() => trace.some(t => t.event === 'resync_snapshot' && t.connected === true && t.at >= gapAt), Boolean, 'actual latched-connected resync');
      await sleep(300);
      assert.equal(rpc.length, 1, 'no recovery send while daemon authentication is unavailable despite latched connected');
      trace.push({event:'latched_connected_did_not_send',at:Date.now()});
      await startDaemon();
    } else {
      await sleep(Math.max(0, acceptedAt + (process.argv.includes("--quick") ? 1e3 : 61e3) - Date.now()));
      await stop(web);
      await stop(daemon);
      await startDaemon();
      await startWeb();
      blocked = false;
    }
    await wait(() => rpc.length, (n) => n >= 2, "automatic same-ID recovery", 3e4);
    if (manualOverlap) {
      await page.click('#cell-0 .slot-chat-send-retry[data-optimistic-id="' + lost.optimisticId + '"]');
      await sleep(100);
      assert.equal(rpc.length, 2, 'manual Retry joins in-flight automatic reconciliation');
      trace.push({event: 'real_DOM_manual_retry_joined', at: Date.now()});
      allowProof = true;
    }
    await wait(() => trace.some(t => t.event === 'daemon_frame' && t.frame.request_id === lost.requestId
      && (t.frame.coalesced === true || t.frame.assistant_composite?.duplicate === true)), Boolean, 'durable original receipt replay', 30000);
    await wait(dom, (d2) => !Object.values(d2.optimistic || {}).some((o) => o.request_id === lost.requestId && ["indeterminate", "failed"].includes(o.status)), "receipt reconciles uncertainty", 3e4);
    await sleep(500);
    const d = await dom(), original = wire.filter((w) => w.frame.request_id === lost.requestId);
    assert.equal(submissions().length, baseline + 1);
    const nativeOriginal = submissions().filter(s => s.text.includes(text));
    assert.equal(nativeOriginal.length, 1, 'the original input has one native-format submission');
    if (kind === 'composite') assert(nativeOriginal[0].text.includes(lost.optimisticId), 'native composite dispatch carries original input identity');
    assert.equal(new Set(rpc.map((r) => r.frame.args[3])).size, 1);
    assert.equal(new Set(rpc.map((r) => r.sha256)).size, 1);
    assert.equal(new Set(original.map((r) => r.sha256)).size, 1);
    assert.equal(d.rows.filter((t) => t.includes(text)).length, 1);
    if (!process.argv.includes("--quick") && !daemonGap) assert(Date.now() - acceptedAt > 6e4);
    assert(trace.some((t) => t.frame?.request_id === lost.requestId && (t.frame.coalesced === true || t.frame.assistant_composite?.duplicate === true || String(t.frame.reason).includes("coalesced"))));
    const escapes = fs.existsSync(out + "/escapes.jsonl") ? fs.readFileSync(out + "/escapes.jsonl", "utf8").trim().split("\n").filter(Boolean).length : 0;
    assert.equal(escapes, 0, "all operator paths use ordinary intake, including retired urgency replacement");
    assert(original.every((w) => !Object.hasOwn(w.frame, "urgent")), "operator wire cannot carry urgency");
    const receipts = trace.filter((t) => t.event === "daemon_frame" && t.frame.type === "send.result" && t.frame.request_id === lost.requestId).map((t) => t.frame.receipt_id);
    assert.equal(new Set(receipts).size, 1, "same durable receipt");
    assert(!d.body.includes("Synthetic reply:"), "no false model completion before provider releases response");
    const originalProof = { elapsedMs: Date.now() - acceptedAt, rpc: rpc.slice(), wire: wire.slice(), submissions: submissions(), dom: d, urgentEscapes: escapes };
    // A deliberate new input with identical text is distinct from recovery.
    await page.type('#cell-0 .slot-chat-compose-input', text);
    await page.click('#cell-0 .slot-chat-compose-send');
    await wait(submissions, v => v.length === baseline + 2, 'new intentional same-text input adds one submission');
    assert.notEqual(rpc.at(-1).frame.args[3], lost.requestId);
    assert.notEqual(rpc.at(-1).frame.args[4], lost.optimisticId);
    write("verdict.json", { status: process.argv.includes("--quick") ? "DEVELOPMENT_PASS" : "PASS", kind, cell: daemonGap ? "R4" : "R3", physicalOriginalSubmissions: 1, baselineDistinctSubmissions: baseline, urgentEscapes: escapes, responseWithheld: true, manualOverlap, originalProof, elapsedMs: originalProof.elapsedMs, lost, rpc, wire, trace, submissions: submissions(), dom: await dom(), webPids, daemonPids });
  } catch (e) {
    write("failure.json", { classification, error: e.stack, trace, wire, rpc, submissions: submissions(), store: page ? await page.eval("({state:window.PentacleChatStore?.getState(),detail:window.PentacleChatStore?.selectSessionDetail(" + JSON.stringify(stream) + ")})").catch(() => null) : null, dom: page ? await dom().catch(() => null) : null, rawBody: page ? await page.eval("document.body.innerText").catch(() => null) : null, console: page?.consoleLines });
    throw e;
  } finally {
    page?.close();
    await stop(browser);
    await stop(web);
    for (const s of sockets) s.terminate();
    if (wproxy) await new Promise((r) => wproxy.close(r));
    if (hproxy) await new Promise((r) => hproxy.close(r));
    if (dproxy) await new Promise((r) => dproxy.close(r));
    await stop(daemon);
    if (tmuxStarted) spawnSync(tmuxBin, ["kill-server"]);
    const token = out + "/operator-auth/token";
    if (fs.existsSync(token)) fs.unlinkSync(token);
    write("cleanup.json", { daemonStopped: !daemon || daemon.exitCode !== null || !!daemon.signalCode, webStopped: !web || web.exitCode !== null || !!web.signalCode, browserStopped: !browser || browser.exitCode !== null || !!browser.signalCode, tmuxGone: spawnSync(tmuxBin, ["list-sessions"]).status !== 0, proxiesClosed: true, tokenRemoved: !fs.existsSync(token), runtimeIsolated: true });
  }
}
main().catch((e) => {
  console.error(e.stack);
  process.exitCode = 1;
});
