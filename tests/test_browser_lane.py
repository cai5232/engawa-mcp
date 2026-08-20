"""Hermetic browser-lane tests: a local HTTP server + the real Chromium.

Skipped automatically when the optional browser extra isn't installed.
ENGAWA_ALLOW_PRIVATE_URLS is monkeypatched on so the guard lets the fixtures
through — the interception mechanics under test are the same either way.
"""
import http.server
import threading

import pytest

from engawa_mcp import config, fetching

playwright = pytest.importorskip("playwright.async_api")

PAGE = b"""<!doctype html><html><head><title>Fixture</title></head><body>
<article><p id="body-text">porch fixture body text, long enough to matter.</p></article>
<script src="/app.js"></script>
<script>
  try {
    const ws = new WebSocket("ws://HOSTPORT/ws");
    ws.onopen = () => { document.title = "WS-OPENED"; };
  } catch (e) {}
  window.__ice = [];
  try {
    const pc = new RTCPeerConnection({iceServers: [{urls: "stun:127.0.0.1:3478"}]});
    pc.createDataChannel("x");
    pc.onicecandidate = (e) => { if (e.candidate) window.__ice.push(e.candidate.candidate); };
    pc.createOffer().then(o => pc.setLocalDescription(o));
  } catch (e) {}
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        if self.path == "/app.js":
            body = b'document.getElementById("body-text").dataset.js = "ran";'
            ctype = "text/javascript"
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        else:
            host = self.headers.get("Host", "")
            body = PAGE.replace(b"HOSTPORT", host.encode())
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def local_site(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", True)
    _Handler.hits = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.browser
async def test_render_and_subresources_intercepted(local_site):
    html, err = await fetching._fetch_dynamic(local_site + "/")
    assert err == "" and "porch fixture body text" in html
    # The subresource was requested — through the route bridge, not around it.
    assert "/app.js" in _Handler.hits
    assert 'data-js="ran"' in html


@pytest.mark.browser
async def test_websocket_never_reaches_server(local_site):
    """The mocked socket may look open to the page (that is how Playwright's
    WebSocket routing works) — the property that matters is that no upgrade
    request ever reaches the network."""
    html, err = await fetching._fetch_dynamic(local_site + "/")
    assert err == ""
    assert all("/ws" not in h for h in _Handler.hits)


@pytest.mark.browser
async def test_webrtc_gathers_no_candidates(local_site):
    """With the WebRTC launch policy, ICE gathering must produce nothing —
    no candidates means no STUN packets left the renderer."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                  "--disable-quic"],
        )
        try:
            page = await browser.new_page()
            await page.goto(local_site + "/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            ice = await page.evaluate("window.__ice")
        finally:
            await browser.close()
    assert ice == []


@pytest.mark.browser
async def test_redirect_collapses_but_renders(local_site):
    html, err = await fetching._fetch_dynamic(local_site + "/redirect")
    assert err == "" and "porch fixture body text" in html


@pytest.mark.browser
async def test_private_target_blocked_when_guard_on(local_site, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    html, err = await fetching._fetch_dynamic(local_site + "/")
    assert html == ""
