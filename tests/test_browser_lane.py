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
<div id="rtc"></div>
<script>
  try {
    const ws = new WebSocket("ws://HOSTPORT/ws");
    ws.onopen = () => { document.title = "WS-OPENED"; };
  } catch (e) {}
  document.getElementById("rtc").textContent =
    "RTC:" + (typeof RTCPeerConnection) + " WT:" + (typeof WebTransport);
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
async def test_webrtc_and_webtransport_apis_removed(local_site):
    """Through the production path (_fetch_dynamic, real CHROMIUM_ARGS + the
    init script), the page must see no RTCPeerConnection and no WebTransport
    at all — no API, no TURN-over-TCP, no STUN. The marker is written
    synchronously, so there is no timing to race."""
    html, err = await fetching._fetch_dynamic(local_site + "/")
    assert err == ""
    assert "RTC:undefined WT:undefined" in html


@pytest.mark.browser
async def test_redirect_collapses_but_renders(local_site):
    html, err = await fetching._fetch_dynamic(local_site + "/redirect")
    assert err == "" and "porch fixture body text" in html


@pytest.mark.browser
async def test_private_target_blocked_when_guard_on(local_site, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", False)
    html, err = await fetching._fetch_dynamic(local_site + "/")
    assert html == ""
