"use strict";
const fs = require("node:fs"), path = require("node:path"), crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");
const { REPAIR_APP: app, REPAIR_SHA: sha, REPAIR_PID: pid, REPAIR_NEXT_SHA256: bundle, REPAIR_ORIGIN: origin, REPAIR_PROOF_DIR: out } = process.env;
if (!app || !/^[a-f0-9]{40}$/.test(sha || "") || !/^\d+$/.test(pid || "") || !/^[a-f0-9]{64}$/.test(bundle || "") || !/^https:\/\//.test(origin || "") || !out) throw Error("Missing release-bound proof configuration");
const results = { started: (new Date()).toISOString(), sha, pid: Number(pid), bundle, steps: [], status: "running" };
fs.mkdirSync(out, { recursive: true });
const save = () => fs.writeFileSync(path.join(out, "proof.json"), JSON.stringify(results, null, 2) + "\n");
const step = (name) => {
  results.steps.push(name);
  save();
};
function bind() {
  const real = fs.realpathSync(app);
  if (!real.includes(sha)) throw Error("release path mismatch");
  const cmd = execFileSync("ps", ["-p", pid, "-o", "command="], { encoding: "utf8" });
  if (!cmd.includes(path.join(real, "server"))) throw Error("PID release mismatch");
  if (!execFileSync("lsof", ["-nP", "-iTCP:7797", "-sTCP:LISTEN", "-Fp"], { encoding: "utf8" }).split("\n").includes("p" + pid)) throw Error("listener mismatch");
  if (crypto.createHash("sha256").update(fs.readFileSync(path.join(real, "renderer/dist/web/next/bundle.js"))).digest("hex") !== bundle) throw Error("bundle mismatch");
}
(async () => {
  let browser;
  let currentPage;
  try {
    bind();
    step("release and listener bound");
    browser = await chromium.launch({ headless: true, ...process.env.CHROME_EXECUTABLE_PATH ? { executablePath: process.env.CHROME_EXECUTABLE_PATH } : { channel: "chrome" }, args: ["--no-sandbox"] });
    for (const width of [1440, 440]) {
      const page = currentPage = await browser.newPage({ viewport: { width, height: 900 } });
      let r = await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
      if (r.status() !== 200) throw Error("root HTTP " + r.status());
      await page.waitForSelector("#sidebar-toggle", { timeout: 3e4 });
      if (!await page.locator("#btn-new").isVisible()) await page.locator("#sidebar-toggle").click();
      await page.locator("#btn-new").click();
      const machines = await page.locator("#new-session-location [data-loc]").evaluateAll((xs) => xs.map((x) => ({ id: x.dataset.loc, label: x.querySelector(".new-session-option-label").textContent.trim() })));
      await page.locator("#new-session-location [data-loc]").first().click();
      await page.waitForFunction(() => document.querySelectorAll("#spawn-provider option").length > 0 && !document.querySelector("#new-session-location").textContent.includes("Loading available spawn profiles"), null, { timeout: 3e4 });
      const catalog = (await page.evaluate(() => window.cc.chatSpawnCatalog())).catalog;
      const allowedModels = catalog.available_models || catalog.models || {};
      const expectedProviders = Object.keys(allowedModels).filter((k) => Array.isArray(catalog.profiles?.desktop_manual?.[k]) && Object.keys(allowedModels[k] || {}).length);
      await page.waitForFunction((expected) => JSON.stringify([...document.querySelectorAll("#spawn-provider option")].map((x) => x.value)) === JSON.stringify(expected), expectedProviders, { timeout: 1e4 });
      const providers = await page.locator("#spawn-provider option").evaluateAll((xs) => xs.map((x) => x.value));
      const oldChoices = {};
      results[`catalogComparison${width}`] = {};
      for (const provider of providers) {
        const started = Date.now();
        await page.locator("#spawn-provider").selectOption(provider);
        const initial = await page.locator("#spawn-model option").evaluateAll((xs) => xs.map((x) => x.value));
        const expected = Object.keys(allowedModels[provider] || {});
        await page.waitForFunction((expected2) => JSON.stringify([...document.querySelectorAll("#spawn-model option")].map((x) => x.value)) === JSON.stringify(expected2), expected, { timeout: 1e4 });
        oldChoices[provider] = await page.evaluate(() => ({ models: [...document.querySelectorAll("#spawn-model option")].map((x) => x.value), efforts: [...document.querySelectorAll("#spawn-effort option")].map((x) => x.value), accounts: [...document.querySelectorAll("#spawn-account option")].map((x) => x.value) }));
        results[`catalogComparison${width}`][provider] = { initial, settled: oldChoices[provider].models, settledMs: Date.now() - started };
        save();
      }
      await page.screenshot({ path: path.join(out, `current-choices-${width}.png`) });
      step("current choices " + width);
      r = await page.goto(origin + "/next/", { waitUntil: "domcontentloaded" });
      if (r.status() !== 200) throw Error("next HTTP " + r.status());
      await page.waitForSelector(".next-composer-input", { timeout: 6e4 });
      const served = await page.evaluate(async () => {
        const r2 = await fetch("/next/bundle.js", { cache: "no-store" });
        const bytes = await r2.arrayBuffer();
        return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((x) => x.toString(16).padStart(2, "0")).join("");
      });
      if (served !== bundle) throw Error("served bundle mismatch");
      await page.locator(".next-composer-input").first().click();
      const visual = await page.evaluate(() => {
        const i = document.querySelector(".pane .next-composer-input"), s = document.querySelector(".pane .thread"), d = document.createElement("div");
        d.style.cssText = "height:2000px;flex:none";
        d.textContent = "Scroll preview";
        s.append(d);
        return { outline: getComputedStyle(i).outlineStyle, scrollbar: getComputedStyle(s).scrollbarWidth, overflow: s.scrollHeight > s.clientHeight };
      });
      if (visual.outline !== "none" || visual.scrollbar !== "thin" || !visual.overflow) throw Error("focus/scroll proof failed");
      await page.screenshot({ path: path.join(out, `next-focus-scroll-${width}.png`) });
      step("focus and scrollbar " + width);
      await page.evaluate(() => {
        window.__proofStatsClick = performance.now();
        window.__proofFirstMachine = null;
        const observer = new MutationObserver(() => {
          if (window.__proofFirstMachine === null && document.querySelector(".limits-view .hrow:not(.hd)")) {
            window.__proofFirstMachine = performance.now();
            observer.disconnect();
          }
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
        window.__proofStatsObserver = observer;
      });
      await page.locator('[aria-label="Limits and machines"]').click();
      await page.screenshot({ path: path.join(out, `next-stats-click-${width}.png`) });
      try {
        if (!await page.locator(".limits-view .limits-section h3").isVisible() || !await page.locator(".limits-view .machines-section h3").isVisible()) throw Error("stats structure missing");
        await page.waitForSelector(".limits-view .hrow:not(.hd)", { state: "attached", timeout: 1e4 });
        results[`firstMachineMs${width}`] = await page.evaluate(() => Math.round((window.__proofFirstMachine ?? performance.now()) - window.__proofStatsClick));
      } finally {
        await page.screenshot({ path: path.join(out, `next-stats-${width}.png`) });
        await page.evaluate(() => window.__proofStatsObserver?.disconnect());
        save();
      }
      step("stats " + width);
      await page.locator('[aria-label="Chats"]').click();
      await page.locator('[aria-label="New chat"]').click();
      await page.waitForSelector('[data-set="provider"]', { timeout: 3e4 });
      const compare = await page.evaluate(async () => {
        const c = (await window.cc.chatSpawnCatalog()).catalog;
        const models = c.available_models || c.models || {}, profile = c.profiles?.desktop_manual || {};
        return { expected: Object.keys(models).filter((k) => Array.isArray(profile[k]) && Object.keys(models[k] || {}).length), actual: [...document.querySelectorAll('[data-set="provider"]')].map((x) => x.dataset.val), models };
      });
      if (JSON.stringify(compare.actual) !== JSON.stringify(compare.expected) || JSON.stringify(compare.actual) !== JSON.stringify(providers)) throw Error("provider choices mismatch");
      for (const provider of providers) {
        await page.locator(`[data-set="provider"][data-val="${provider}"]`).click();
        const actual = await page.locator('[data-set="model"]').evaluateAll((xs) => xs.map((x) => x.dataset.val));
        results[`catalogComparison${width}`][provider].next = actual;
        save();
        if (JSON.stringify(actual) !== JSON.stringify(oldChoices[provider].models)) throw Error("model choices mismatch: " + provider);
        if (actual.length) {
          await page.locator(`[data-set="model"][data-val="${actual[0]}"]`).click();
          const options = await page.evaluate(() => ({ efforts: [...document.querySelectorAll("[data-set=effort]")].map((x) => x.dataset.val), accounts: [...document.querySelectorAll("[data-set=account]")].map((x) => x.dataset.val), machines: [...document.querySelectorAll("[data-set=host]")].map((x) => ({ id: x.dataset.val, label: x.textContent.trim() })) }));
          if (JSON.stringify(options.efforts) !== JSON.stringify(oldChoices[provider].efforts) || JSON.stringify(options.accounts) !== JSON.stringify(oldChoices[provider].accounts) || JSON.stringify(options.machines) !== JSON.stringify(machines)) throw Error("effort/account/machine choices mismatch: " + provider);
        }
      }
      await page.screenshot({ path: path.join(out, `next-choices-${width}.png`) });
      step("available choices " + width);
      r = await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
      if (r.status() !== 200) throw Error("root HTTP " + r.status());
      await page.waitForSelector("#sidebar-toggle", { timeout: 3e4 });
      await page.screenshot({ path: path.join(out, `current-root-${width}.png`) });
      step("current web " + width);
      await page.close();
      currentPage = null;
    }
    bind();
    results.status = "pass";
    results.finished = (new Date()).toISOString();
    save();
  } catch (e) {
    if (currentPage && !currentPage.isClosed()) {
      try { await currentPage.screenshot({ path: path.join(out, "failure.png") }); } catch (_) { /* Preserve the original failure. */ }
    }
    results.status = "fail";
    results.error = String(e.message || e);
    results.finished = (new Date()).toISOString();
    save();
    process.exitCode = 1;
  } finally {
    await browser?.close();
  }
})();
