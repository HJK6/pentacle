"""Cross-origin browser fixture for the Pentacle Travel Engine dashboard.

This serves the real Pentacle adapter and Travel Engine page over separate
loopback HTTP origins. Only in-memory API fixtures are provided; the real
FastAPI application, provider profiles, credentials, and purchase endpoint are
never started.
"""
from __future__ import annotations

import json
import mimetypes
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT.parent / "travel-engine"
ENGINE_WEB = ENGINE / "web"
SNAPSHOT_BUILDER = ENGINE / "scripts" / "build_ui_snapshot.py"
ENGINE_PYTHON = ENGINE / ".venv" / "bin" / "python"
ADAPTER = ROOT / "renderer" / "dashboards" / "travel_engine.js"
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDAT\x08\xd7c\xf8\xcf\xc0\xf0"
    b"\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FixtureState:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.balances = {
            "Chase Ultimate Rewards": {
                "points": 15000, "kind": "bank", "source": "fixture", "updated_at": 1790000000,
            },
        }
        self.vault_initialized = False
        self.vault_unlocked = False
        self.cards = [{
            "id": "fixture-card", "nickname": "Fixture Visa", "brand": "visa",
            "last4": "4242", "exp": "12/30",
        }]
        self.names = ["frontier_login", "amex_login", "frontier_payment", "email_imap"]

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append((method, path))


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status: int = 200) -> None:
        self.send_bytes(json.dumps(value).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")


class EngineHandler(QuietHandler):
    state: FixtureState
    web_root: Path

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.state.record("GET", path)
        if path == "/api/balances" or path == "/api/balances/refresh":
            self.send_json({"balances": self.state.balances})
        elif path == "/api/vault/status":
            self.send_json({"initialized": self.state.vault_initialized, "unlocked": self.state.vault_unlocked})
        elif path == "/api/vault/cards":
            self.send_json(self.state.cards if self.state.vault_unlocked else [])
        elif path == "/api/vault/secrets":
            self.send_json({"names": self.state.names if self.state.vault_unlocked else []})
        elif path.startswith("/api/searches/") and path.endswith("/share"):
            self.send_json({"detail": "share is POST only"}, 405)
        elif path.startswith("/api/searches/"):
            self.send_json(self.state.snapshot)
        elif path == "/api/book/fixture-booking":
            self.send_json({"status": "REVIEW_READY", "review": {"total_usd": 132.40, "total_points": None}})
        elif path == "/api/book/fixture-booking/shot":
            self.send_bytes(TINY_PNG, "image/png")
        elif path.startswith("/api/"):
            self.send_json({"detail": "fixture route not found"}, 404)
        else:
            self.serve_static(path)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        self.state.record("POST", path)
        body = self.read_json()
        if path == "/api/searches":
            self.send_json({"search_id": self.state.snapshot["id"]})
        elif path.startswith("/api/searches/") and path.endswith("/share"):
            self.send_json({"url": "http://127.0.0.1/share-fixture"})
        elif path == "/api/balances/manual":
            program = str(body.get("program") or "Fixture Rewards")
            self.state.balances[program] = {
                "points": float(body.get("points", 0)),
                "kind": str(body.get("kind") or "other"),
                "source": "manual fixture",
                "updated_at": 1790000000,
            }
            self.send_json({"ok": True})
        elif path == "/api/vault/init":
            self.state.vault_initialized = True
            self.send_json({"ok": True})
        elif path == "/api/vault/unlock":
            self.state.vault_unlocked = True
            self.send_json({"ok": True})
        elif path == "/api/vault/lock":
            self.state.vault_unlocked = False
            self.send_json({"ok": True})
        elif path == "/api/vault/test-email":
            self.send_json({"ok": True})
        elif path == "/api/vault/cards":
            self.send_json({"ok": True, "id": "fixture-card"})
        elif path == "/api/vault/secrets":
            name = str(body.get("name") or "fixture_login")
            if name not in self.state.names:
                self.state.names.append(name)
            self.send_json({"ok": True})
        elif path == "/api/book":
            self.send_json({"booking_id": "fixture-booking"})
        elif path.startswith("/api/book/") and path.endswith("/confirm"):
            self.send_json({"detail": "purchase confirmation disabled in fixture"}, 405)
        else:
            self.send_json({"detail": "fixture route not found"}, 404)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        self.state.record("DELETE", path)
        self.send_json({"ok": True})

    def serve_static(self, url_path: str) -> None:
        relative = unquote(url_path).lstrip("/") or "index.html"
        candidate = (self.web_root / relative).resolve()
        if not candidate.is_relative_to(self.web_root.resolve()) or not candidate.is_file():
            self.send_json({"detail": "fixture asset not found"}, 404)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_bytes(candidate.read_bytes(), content_type)


class ParentHandler(QuietHandler):
    pentacle_root: Path
    engine_url: str

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/dashboards/travel_engine.js":
            self.send_bytes(self.pentacle_root.joinpath("renderer/dashboards/travel_engine.js").read_bytes(), "text/javascript; charset=utf-8")
            return
        if path not in ("/", "/same-origin"):
            self.send_bytes(b"not found", "text/plain", 404)
            return
        engine_url = self.engine_url
        if path == "/same-origin":
            engine_url = f"http://127.0.0.1:{self.server.server_port}/"
        page = f"""<!doctype html>
<meta charset="utf-8">
<title>Pentacle dashboard fixture</title>
<main id="mount"></main>
<script>
window.DASHBOARDS = [];
window.HOST = {{ travelEngineConfig: {{ url: {json.dumps(engine_url)} }} }};
window.__openedExternal = [];
window.cc = {{ openExternal: url => {{ window.__openedExternal.push(url); return Promise.resolve({{ok:true}}); }} }};
</script>
<script src="/dashboards/travel_engine.js"></script>
<script>
window.__travelDashboard = window.DASHBOARDS.find(board => board.id === 'travel-engine');
window.__travelRefs = window.__travelDashboard.mount(document.getElementById('mount'));
</script>""".encode("utf-8")
        self.send_bytes(page, "text/html; charset=utf-8")


def start_server(handler, **attrs) -> ThreadingHTTPServer:
    bound_handler = type("BoundFixtureHandler", (handler,), attrs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bound_handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._fixture_thread = thread
    return server


def get_engine_frame(page, origin: str):
    for frame in page.frames:
        if frame.url.startswith(origin):
            return frame
    raise AssertionError(f"Travel Engine frame not found for {origin}: {[frame.url for frame in page.frames]}")


def main() -> None:
    if not ENGINE_PYTHON.exists():
        raise RuntimeError(f"Travel Engine virtualenv is required: {ENGINE_PYTHON}")
    with tempfile.TemporaryDirectory(prefix="pentacle-travel-fixture-") as temp_dir:
        snapshot_path = Path(temp_dir) / "ui_snapshot.json"
        built = subprocess.run(
            [str(ENGINE_PYTHON), str(SNAPSHOT_BUILDER), str(snapshot_path)],
            cwd=ENGINE,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        snapshot = json.loads(snapshot_path.read_text())
        state = FixtureState(snapshot)

        engine_server = start_server(EngineHandler, state=state, web_root=ENGINE_WEB)
        engine_origin = f"http://127.0.0.1:{engine_server.server_port}"
        parent_server = start_server(ParentHandler, pentacle_root=ROOT, engine_url=engine_origin + "/")
        parent_origin = f"http://127.0.0.1:{parent_server.server_port}"
        checks: list[str] = []

        def check(condition: bool, message: str) -> None:
            if not condition:
                raise AssertionError(message)
            checks.append(message)
            print(f"ok  {message}")

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})

                def block_external_network(route) -> None:
                    host = urlsplit(route.request.url).hostname
                    if host in ("127.0.0.1", "localhost"):
                        route.continue_()
                    else:
                        route.abort()

                context.route("**/*", block_external_network)
                page = context.new_page()
                page.goto(parent_origin, wait_until="domcontentloaded")
                page.wait_for_function("() => { const f=document.querySelector('iframe.travel-engine-frame'); return f && !f.hidden; }", timeout=15000)
                frame = get_engine_frame(page, engine_origin)
                frame.locator("#from").wait_for(state="visible")
                check(frame.locator("#viewtoggle").inner_text().find("Wallet") >= 0, "original Travel Engine page reached the parent ready handshake")

                frame.locator("#from").fill("a")
                frame.locator("#ac-from").wait_for(state="visible")
                counts = frame.evaluate("""() => {
                  const rows = [...document.querySelectorAll('#ac-from .ac-item')];
                  const codes = rows.filter(row => row.dataset.code.toLowerCase().startsWith('a'));
                  return {total: rows.length, prefix: codes.length,
                    last: codes.at(-1)?.dataset.code,
                    expected: JSON.parse(document.body.dataset.fixtureExpectedA || '0'),
                    overflow: getComputedStyle(document.querySelector('#ac-from')).overflowY};
                }""")
                expected_a = sum(code.lower().startswith("a") for code, *_ in json.loads((ENGINE_WEB / "airports.json").read_text()))
                check(counts["prefix"] == expected_a and expected_a > 12, "airport-code dropdown contains every one-letter prefix match")
                check(counts["overflow"] == "auto", "full airport-code list uses a scrollable dropdown")
                last_code = counts["last"]
                frame.locator("#from").evaluate("""(input, count) => {
                  for (let i = 0; i < count; i++) input.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown', bubbles:true, cancelable:true}));
                }""", counts["prefix"])
                selected = frame.locator("#ac-from .ac-item.sel")
                check(selected.get_attribute("data-code") == last_code, "Arrow navigation reaches the last airport-code candidate")
                scroll_state = frame.evaluate("""() => {
                  const list = document.querySelector('#ac-from');
                  const item = list.querySelector('.ac-item.sel');
                  const listRect = list.getBoundingClientRect(), itemRect = item.getBoundingClientRect();
                  return {scrollTop:list.scrollTop, visible:itemRect.top >= listRect.top && itemRect.bottom <= listRect.bottom};
                }""")
                check(scroll_state["scrollTop"] > 0 and scroll_state["visible"], "keyboard highlight scrolls into view near the end of the full list")
                frame.locator("#from").press("Enter")
                check(frame.locator("#from").input_value() == last_code, "Enter selects a result beyond the old 12-item cap")

                frame.locator("#from").fill("a")
                frame.locator(f"#ac-from .ac-item[data-code='{last_code}']").click()
                check(frame.locator("#from").input_value() == last_code, "click selects a near-end airport candidate")
                # Allow the input blur handler from the previous selection to
                # finish before reopening the list for a second query.
                frame.wait_for_timeout(200)
                metro_code = json.loads((ENGINE_WEB / "metros.json").read_text())[0][0]
                frame.locator("#from").fill(metro_code[:3].lower())
                frame.locator(f"#ac-from .ac-item.metro[data-code='{metro_code}']").click()
                check(frame.locator("#from").input_value() == metro_code, "metro-code candidates remain selectable")

                frame.locator("#from").fill("DEN")
                frame.locator("#to").fill("LAS")
                frame.locator("#go").click()
                frame.locator("#results .opt").first.wait_for(state="visible", timeout=10000)
                source_text = frame.locator("#sources").inner_text()
                for source in ("Frontier", "Southwest", "PointsYeah", "Skiplagged", "Momondo", "Google"):
                    check(source in source_text, f"search results include the {source} source category")
                points_count = frame.locator("#results .opt").count()
                check(points_count > 20 and "pts" in frame.locator("#results .opt .v").first.inner_text(), "Points results render from the fixture snapshot")

                frame.locator(".tab[data-tab='dollars']").click()
                check("$" in frame.locator("#results .opt .v").first.inner_text(), "Dollars results render in the original page")
                frame.locator(".tab[data-tab='points']").click()
                airline_filter = frame.locator(".fchip[data-fkind='airlines']").first
                before_filter = frame.locator("#results .opt").count()
                airline_filter.click()
                check(frame.locator("#results .opt").count() < before_filter, "embedded airline filters update results")
                frame.locator("#fclear").click()
                frame.locator("#layout button[data-layout='list']").click()
                check(frame.locator("#results .card").count() > 10, "embedded list layout renders itinerary cards")
                frame.locator("#layout button[data-layout='grid']").click()
                frame.locator("#results .opt").first.click()
                check(frame.locator("#itindrawer.open .leg").count() > 0, "embedded itinerary detail opens with flight legs")

                frame.locator("#itindrawer .dclose").click()
                frame.locator("#sharebtn").click()
                frame.locator("#searchstage .sharelink input").wait_for(state="visible")
                check("share-fixture" in frame.locator("#searchstage .sharelink input").input_value(), "sharing reaches the fixture API and displays its link")

                frame.locator("#viewtoggle button[data-view='wallet']").click()
                frame.locator("#wgrid .wcard").first.wait_for(state="visible")
                check("Chase Ultimate Rewards" in frame.locator("#wgrid").inner_text(), "Wallet reads fixture balances")
                frame.locator("#wgrid [data-act='add-open']").click()
                frame.locator("#newProgram").fill("Fixture Rewards")
                frame.locator("#newPoints").fill("7250")
                frame.locator("#newKind").select_option("bank")
                frame.locator("#newSave").click()
                frame.locator("#wgrid [data-program='Fixture Rewards']").wait_for(state="visible")
                check("Fixture Rewards" in frame.locator("#wgrid").inner_text(), "Wallet saves and reloads a manual balance through the stub")

                frame.locator("#viewtoggle button[data-view='vault']").click()
                frame.locator("#vp1").wait_for(state="visible")
                frame.locator("#vp1").fill("fixture-only-passphrase")
                frame.locator("#vp2").fill("fixture-only-passphrase")
                frame.locator("#vaultCreateBtn").click()
                frame.locator("#vaultview .vaulthead").wait_for(state="visible")
                vault_text = frame.locator("#vaultview").inner_text()
                for label in ("Fixture Visa", "Frontier", "American Express", "email configured"):
                    check(label in vault_text, f"Vault renders fixture {label} state")
                frame.locator("#emailTestBtn").click()
                frame.wait_for_function(
                    "() => document.querySelector('#emailTestResult')?.textContent.includes('Connected')",
                    timeout=5000,
                )
                check("Connected" in frame.locator("#emailTestResult").inner_text(), "Vault email test control reaches the in-memory API stub")
                frame.locator("#vaultLockBtn").click()
                frame.locator("#vpUnlock").wait_for(state="visible")
                frame.locator("#vpUnlock").fill("fixture-only-passphrase")
                frame.locator("#vaultUnlockBtn").click()
                frame.locator("#vaultview .vaulthead").wait_for(state="visible")
                check("Fixture Visa" in frame.locator("#vaultview").inner_text(), "Vault unlock restores its saved-item view")

                frame.locator("#viewtoggle button[data-view='search']").click()
                frame.evaluate("""() => {
                  const option = [...document.querySelectorAll('#results .opt')]
                    .find(el => el.querySelector('.prog')?.textContent.includes('Alaska'));
                  if (!option) throw new Error('PointsYeah fixture offer not found');
                  option.click();
                }""")
                deep_link = frame.locator("#itinbody a.bookbtn[href^='http']").first
                deep_link.wait_for(state="visible")
                deep_link.click()
                page.wait_for_function("() => window.__openedExternal.length === 1")
                opened = page.evaluate("window.__openedExternal[0]")
                check(opened.startswith("http"), "deep-link booking opens through the Pentacle adapter stub")

                frame.evaluate("""() => {
                  const option = [...document.querySelectorAll('#results .opt')]
                    .find(el => el.querySelector('.prog')?.textContent.includes('Frontier'));
                  if (!option) throw new Error('Frontier fixture offer not found');
                  option.click();
                }""")
                frame.locator("#itinbody button.bookbtn[onclick^='openBooking']").first.click()
                frame.locator("#modal button.assemble").wait_for(state="visible")
                frame.locator("#modal button.assemble").click()
                frame.locator("#mbody h4").filter(has_text="Frontier's review screen").wait_for(state="visible", timeout=5000)
                check(frame.locator("#mbody button.assemble").inner_text().startswith("Confirm & purchase"), "assisted booking reaches the review control")
                frame.locator("#mbody button.ghostbtn").click()
                with state.lock:
                    confirmed = any(method == "POST" and path.endswith("/confirm") for method, path in state.requests)
                check(not confirmed, "booking fixture proves the purchase-confirm endpoint was never called")

                same_page = context.new_page()
                same_page.goto(parent_origin + "/same-origin", wait_until="domcontentloaded")
                check("different origin" in same_page.locator("#mount").inner_text(), "same-origin engine URL is rejected")
                check(same_page.locator("iframe").count() == 0, "same-origin rejection mounts no iframe")
                same_page.close()

                offline_page = context.new_page()
                first_request = {"aborted": False}

                def abort_then_allow(route) -> None:
                    if route.request.url == engine_origin + "/" and not first_request["aborted"]:
                        first_request["aborted"] = True
                        route.abort()
                    else:
                        route.continue_()

                offline_page.route(engine_origin + "/**", abort_then_allow)
                offline_page.goto(parent_origin, wait_until="domcontentloaded")
                offline_page.locator(".travel-engine-status.is-unavailable").wait_for(state="visible", timeout=12000)
                check("Retry" in offline_page.locator(".travel-engine-status.is-unavailable").inner_text(), "network failure reaches timeout and actionable unavailable state")
                offline_page.locator(".travel-engine-retry").click()
                offline_page.wait_for_function("() => { const f=document.querySelector('iframe.travel-engine-frame'); return f && !f.hidden; }", timeout=10000)
                check(True, "Retry reloads the service and accepts its ready handshake")
                offline_page.close()

                with state.lock:
                    unexpected_confirm = [(method, path) for method, path in state.requests if method == "POST" and path.endswith("/confirm")]
                check(not unexpected_confirm, "fixture request log contains no purchase confirmation")
                print(f"checks={len(checks)}; engine API requests={len(state.requests)}")
                context.close()
                browser.close()
        finally:
            parent_server.shutdown()
            parent_server.server_close()
            engine_server.shutdown()
            engine_server.server_close()
            parent_server._fixture_thread.join(timeout=2)
            engine_server._fixture_thread.join(timeout=2)
            print(built.stdout.strip())


if __name__ == "__main__":
    main()
