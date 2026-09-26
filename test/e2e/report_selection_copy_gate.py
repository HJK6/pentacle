#!/usr/bin/env python3
"""Real-browser report selection/copy gate for the built Pentacle web renderer."""

import argparse
import functools
import hashlib
import http.server
import json
import pathlib
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys


ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB = ROOT / "renderer" / "dist" / "web"
TEXT = "Copy this exact report passage before it vanishes."
SECOND = "Second block remains readable after comments."
CONFIG = {"hostname": "local", "features": {"chatUi": True}, "agents": {}, "hosts": {}}


class GateFailure(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def current_source():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


class CandidateHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path in ("/", "/web.html"):
            html = (WEB / "web.html").read_bytes()
            marker = b"<!--PENTACLE_CONFIG-->"
            if marker not in html:
                raise GateFailure("HARNESS_ERROR", "candidate web.html has no config placeholder")
            injected = b"<script>window.__PENTACLE_CONFIG__=" + json.dumps(CONFIG).encode() + b";</script>"
            body = html.replace(marker, injected)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def require(verdict, name, condition, detail=None):
    verdict["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise GateFailure("PRODUCT_FAIL", f"{name}: {detail}")


def js(driver, script, *args):
    return driver.execute_script(script, *args)


def selection(driver):
    return js(driver, "return getSelection().toString()")


def point(driver, block_id, offset):
    return js(driver, """
      const p=document.querySelector('#report-copy-gate [data-block-id="'+arguments[0]+'"] .slot-asset-report-block-body p');
      if (!p || !p.firstChild || p.firstChild.nodeType !== Node.TEXT_NODE) return null;
      const range=document.createRange();
      range.setStart(p.firstChild,arguments[1]);range.setEnd(p.firstChild,arguments[1]+1);
      const glyph=range.getBoundingClientRect(), whole=p.getBoundingClientRect();
      return {x:glyph.x+1,y:glyph.y+glyph.height/2,cx:whole.x+whole.width/2,cy:whole.y+whole.height/2};
    """, block_id, offset)


def move_to_point(actions, element, pt):
    return actions.move_to_element_with_offset(element, round(pt["x"] - pt["cx"]), round(pt["y"] - pt["cy"]))


def drag(driver, start_block, start_offset, end_block, end_offset):
    a = point(driver, start_block, start_offset)
    b = point(driver, end_block, end_offset)
    if not a or not b:
        raise GateFailure("PRODUCT_FAIL", "report text nodes for pointer drag are missing")
    first = driver.find_element(By.CSS_SELECTOR, f'#report-copy-gate [data-block-id="{start_block}"] .slot-asset-report-block-body p')
    last = driver.find_element(By.CSS_SELECTOR, f'#report-copy-gate [data-block-id="{end_block}"] .slot-asset-report-block-body p')
    move_to_point(ActionChains(driver), first, a).click_and_hold().move_to_element_with_offset(
        last, round(b["x"] - b["cx"]), round(b["y"] - b["cy"])).perform()
    held = js(driver, "return {text:getSelection().toString(),anchor:getSelection().anchorOffset,focus:getSelection().focusOffset,connected:window.__reportGateNode.isConnected}")
    ActionChains(driver).release().perform()
    released = js(driver, "return {text:getSelection().toString(),connected:window.__reportGateNode.isConnected}")
    return held, released


def clipboard(driver):
    return driver.execute_async_script("""
      const done=arguments[0];
      navigator.clipboard.readText().then(text=>done({ok:true,text}),err=>done({ok:false,error:err.name+':'+err.message}));
    """)


def run_browser(driver, verdict):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if js(driver, "return document.readyState==='complete' && typeof window.PentacleReportRender==='object'"):
            break
        time.sleep(0.1)
    else:
        raise GateFailure("HARNESS_ERROR", "built page did not load PentacleReportRender")

    origin = f"{urlsplit(driver.current_url).scheme}://{urlsplit(driver.current_url).netloc}"
    driver.execute_cdp_cmd("Browser.grantPermissions", {"origin": origin, "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]})
    ActionChains(driver).move_to_element_with_offset(driver.find_element(By.TAG_NAME, "body"), 5, 5).click().perform()
    control = driver.execute_async_script("""
      const done=arguments[0];navigator.clipboard.writeText('REPORT_GATE_SENTINEL')
        .then(()=>navigator.clipboard.readText()).then(text=>done({ok:true,text}),err=>done({ok:false,error:err.name+':'+err.message}));
    """)
    if not control.get("ok") or control.get("text") != "REPORT_GATE_SENTINEL":
        raise GateFailure("HARNESS_ERROR", f"isolated clipboard control failed: {control}")

    mounted = js(driver, """
      const report={schema_version:1,title:'Selection diagnostic',sections:[{id:'sec',title:'Findings',status:'in_progress',blocks:[
        {id:'first',type:'para',runs:[{type:'text',text:arguments[0]}]},
        {id:'second',type:'para',runs:[{type:'text',text:arguments[1]}]}
      ]}]};
      const mount=document.createElement('div');mount.id='report-copy-gate';
      mount.style.cssText='position:fixed;z-index:2147483647;top:20px;left:420px;width:850px;height:720px;overflow:auto;background:#12191b;padding:20px;color:white;user-select:text';
      document.body.appendChild(mount);
      const key='spec:report-copy-gate:fixture';
      mount.appendChild(PentacleReportRender.renderReport(document,report,{classPrefix:'slot-asset',asset:{asset_id:'fixture',asset_key:key},comments:[]}));
      window.__reportGateNode=mount.querySelector('[data-block-id="first"] .slot-asset-report-block-body p').firstChild;
      window.__reportGate={mount,report,key};
      return {text:window.__reportGateNode.textContent,connected:window.__reportGateNode.isConnected};
    """, TEXT, SECOND)
    require(verdict, "candidate report mounted", mounted == {"text": TEXT, "connected": True}, mounted)

    held, released = drag(driver, "first", 0, "first", 20)
    chosen = TEXT[:20]
    require(verdict, "pointer drag selects exact text while held", held["text"] == chosen and held["connected"], held)
    require(verdict, "selection survives release click", released["text"] == chosen and released["connected"], released)

    comment = {"comment_id": "c1", "section_id": "sec", "block_id": "first", "excerpt": TEXT,
               "body": "New synthetic comment", "resolved": False}
    refreshed = js(driver, """
      const changed=PentacleReportRender.updateComments(window.__reportGate.key,[arguments[0]]);
      const root=window.__reportGate.mount;
      const block=root.querySelector('[data-block-id="first"]');
      return {changed,text:getSelection().toString(),connected:window.__reportGateNode.isConnected,
        marker:block.querySelector('.slot-asset-report-comment-pin').textContent,
        unresolved:block.classList.contains('has-unresolved-comments')};
    """, comment)
    require(verdict, "comment refresh preserves selection and updates marker", refreshed == {
        "changed": True, "text": chosen, "connected": True, "marker": "1", "unresolved": True}, refreshed)

    ActionChains(driver).key_down(Keys.CONTROL).send_keys("c").key_up(Keys.CONTROL).perform()
    copied = clipboard(driver)
    if not copied.get("ok"):
        raise GateFailure("HARNESS_ERROR", f"isolated clipboard read failed: {copied}")
    require(verdict, "keyboard copy equals selected report text", copied["text"] == chosen, copied)

    js(driver, "getSelection().removeAllRanges()")
    held, released = drag(driver, "first", 5, "second", 12)
    require(verdict, "cross-block pointer selection survives release", bool(held["text"])
            and "report passage" in held["text"] and "Second block" in held["text"]
            and released["text"] == held["text"] and released["connected"], {"held": held, "released": released})

    js(driver, "getSelection().removeAllRanges()")
    first = driver.find_element(By.CSS_SELECTOR, '#report-copy-gate [data-block-id="first"] .slot-asset-report-block-body p')
    ActionChains(driver).click(first).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
    keyboard = selection(driver)
    require(verdict, "keyboard selection contains report blocks", TEXT in keyboard and SECOND in keyboard,
            {"length": len(keyboard), "has_first": TEXT in keyboard, "has_second": SECOND in keyboard})

    js(driver, "getSelection().removeAllRanges()")
    driver.find_element(By.CSS_SELECTOR, '#report-copy-gate [data-block-id="first"] .slot-asset-report-block-body').click()
    composer = js(driver, "return !!document.querySelector('#report-copy-gate .slot-asset-report-panel.is-open .slot-asset-report-comment-input')")
    require(verdict, "ordinary click opens comment composer", composer)

    revision = js(driver, """
      const mount=window.__reportGate.mount, old=mount.querySelector('.slot-asset-report');
      const report={...window.__reportGate.report,sections:[{...window.__reportGate.report.sections[0],blocks:[
        {id:'first',type:'para',runs:[{type:'text',text:'Revised report body is visible.'}]}
      ]}]};
      old.replaceWith(PentacleReportRender.renderReport(document,report,{classPrefix:'slot-asset',asset:{asset_id:'fixture',asset_key:window.__reportGate.key},comments:[]}));
      return {oldConnected:old.isConnected,text:mount.querySelector('[data-block-id="first"] .slot-asset-report-block-body').textContent,selection:getSelection().toString()};
    """)
    require(verdict, "real report revision replaces content", revision == {
        "oldConnected": False, "text": "Revised report body is visible.", "selection": ""}, revision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Use an already served Pentacle web origin for post-activation readback")
    parser.add_argument("--output-dir", type=pathlib.Path)
    args = parser.parse_args()
    out = args.output_dir or pathlib.Path(tempfile.mkdtemp(prefix="pentacle-report-selection-gate-"))
    out.mkdir(parents=True, exist_ok=True)
    verdict = {"scenario": "report_selection_copy", "at": datetime.now(timezone.utc).isoformat(),
               "source_sha": current_source(), "checks": [], "status": "HARNESS_ERROR"}
    server = None
    driver = None
    cleanup = {"browser_closed": False, "server_closed": False}
    try:
        if args.url:
            url = args.url
            bundle_url = urljoin(url if url.endswith("/") else url + "/", "bundle.js")
            bundle = urllib.request.urlopen(bundle_url, timeout=15).read()
        else:
            if not (WEB / "web.html").is_file() or not (WEB / "bundle.js").is_file():
                raise GateFailure("HARNESS_ERROR", "run npm run build:web before the browser gate")
            bundle = (WEB / "bundle.js").read_bytes()
            handler = functools.partial(CandidateHandler, directory=str(WEB))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}/web.html"
        verdict["url"] = url
        verdict["bundle_sha256"] = sha256(bundle)
        with tempfile.TemporaryDirectory(prefix="pentacle-report-chrome-") as profile:
            options = webdriver.ChromeOptions()
            for flag in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1600,1000",
                         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"):
                options.add_argument(flag)
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            driver.set_script_timeout(10)
            verdict["chrome_version"] = driver.capabilities.get("browserVersion")
            verdict["driver_version"] = driver.capabilities.get("chrome", {}).get("chromedriverVersion", "").split(" ")[0]
            driver.get(url)
            run_browser(driver, verdict)
            verdict["status"] = "PASS"
            driver.quit()
            driver = None
            cleanup["browser_closed"] = True
    except GateFailure as error:
        verdict["status"] = error.kind
        verdict["error"] = str(error)
    except Exception as error:
        verdict["status"] = "HARNESS_ERROR"
        verdict["error"] = f"{type(error).__name__}: {error}"
    finally:
        if driver is not None:
            try:
                driver.quit()
                cleanup["browser_closed"] = True
            except Exception as error:
                cleanup["browser_error"] = str(error)
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
                cleanup["server_closed"] = True
            except Exception as error:
                cleanup["server_error"] = str(error)
        else:
            cleanup["server_closed"] = True
        verdict["cleanup"] = cleanup
        if not all(cleanup.values()):
            verdict["status"] = "CLEANUP_FAIL" if verdict["status"] == "PASS" else verdict["status"]
        (out / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
        print(f"report_selection_copy_gate: {verdict['status']}\nartifacts: {out}")
        if verdict.get("error"):
            print(verdict["error"])
    return 0 if verdict["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
