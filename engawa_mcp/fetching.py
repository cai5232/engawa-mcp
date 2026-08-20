"""Page fetching and article extraction for web_read.

Two-lane design: httpx + readability first (covers most article pages in
milliseconds), and if the extracted text is suspiciously short — the classic
SPA signature — fall back to a real browser render via Playwright.

Playwright is an optional extra (`pip install engawa-mcp[browser]`, then
`playwright install chromium`). Without it the static lane still works and
SPA pages report why they came back thin.
"""
from __future__ import annotations

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from readability import Document

from . import net

# A browser-ish UA for the static lane; plenty of sites serve bots a stub.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Static text shorter than this ⇒ probably an SPA shell, try the browser lane.
SPA_CONTENT_THRESHOLD = 500
MAX_LINKS = 20
PAGE_MAX_BYTES = 20 * 1024 * 1024        # per-response cap, streamed in
AGGREGATE_MAX_BYTES = 64 * 1024 * 1024   # per-render cap across all requests

# Production Chromium hardening — module-level so tests pin the real launch
# configuration instead of a copy that could drift.
CHROMIUM_ARGS = [
    # WebRTC can emit STUN over UDP without ever hitting the route layer — a
    # direct network path around the guard. With no proxy configured, this
    # policy silences it (probe-verified: ICE gathering yields zero candidates).
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    # QUIC would let WebTransport talk UDP around the guard.
    "--disable-quic",
    # The renderer needs no DNS at all — every fetch is fulfilled from our
    # side. Mapping every name to NXDOMAIN kills speculative networking
    # (DNS prefetch, preconnect) and anything else that might slip past the
    # route layer by name. Routed+fulfilled requests never resolve, so this
    # costs nothing.
    "--host-resolver-rules=MAP * ~NOTFOUND",
]

# Belt to the flags' braces: remove the WebRTC/WebTransport entry points from
# every document in the context. The IP-handling policy already blocks the
# UDP path; this also closes TURN-over-TCP by leaving no API to reach it.
WEBRTC_KILL_SCRIPT = """
for (const k of ["RTCPeerConnection", "webkitRTCPeerConnection",
                 "RTCIceGatherer", "WebTransport"]) {
  try {
    Object.defineProperty(window, k,
      {value: undefined, configurable: false, writable: false});
  } catch (e) {}
}
"""

# Test seam: injected into guarded_get so the static lane's redirect handling
# is testable with httpx.MockTransport. None in production.
_TRANSPORT_FOR_TESTS = None


async def _fetch_static(url: str) -> tuple[str, str, bool]:
    """Fetch raw HTML through the guarded, DNS-pinned lane (net.guarded_get):
    every redirect hop is vetted before it is followed. Returns
    (html, error_note, renderable); empty html = failure, and renderable=False
    means the browser lane won't help either (a PDF or image is not a page)."""
    try:
        resp, err = await net.guarded_get(url, timeout=20.0,
                                          headers=BROWSER_HEADERS,
                                          max_bytes=PAGE_MAX_BYTES,
                                          transport=_TRANSPORT_FOR_TESTS)
    except httpx.TimeoutException:
        return "", "static fetch timed out", True
    except Exception as e:  # noqa: BLE001
        return "", f"static fetch failed: {type(e).__name__}", True
    if resp is None:
        return "", err or "static fetch failed", True
    if resp.status_code != 200:
        return "", f"HTTP {resp.status_code}", True
    ctype = (resp.headers.get("content-type") or "").lower()
    if ctype and not any(t in ctype for t in ("html", "xml", "text")):
        # Content-type says binary (PDF, image, zip…). The browser lane can't
        # help — a PDF triggers a download, not a page — so don't waste a
        # Chromium launch. But some misconfigured servers send real HTML as
        # application/octet-stream; sniff the first bytes before giving up.
        head = (resp.content or b"")[:512].lstrip().lower()
        if not head.startswith((b"<!doctype", b"<html")):
            return "", f"not a readable web page (content-type: {ctype.split(';')[0].strip()})", False
    return resp.text, "", True


async def _fetch_dynamic(url: str) -> tuple[str, str]:
    """Render with a headless Chromium (Playwright). Returns (html, error_note).

    The context is ephemeral — no profile directory, so no cookies, history,
    or cache survive the call and "stateless" stays literally true.

    Guard posture in this lane: the browser never touches the network. Every
    request the page makes (navigation, subresources, popups, workers' HTTP)
    is intercepted and performed by the same DNS-pinned guarded_get the HTTP
    lane uses, then fulfilled back into the renderer — so the pin, per-hop
    redirect vetting, and size caps all apply here too. WebSockets are blocked
    outright (a read-only article renderer has no business opening one), as
    are non-GET requests and service workers; WebRTC's direct-UDP path and
    QUIC (WebTransport's carrier) are disabled at browser launch, since
    neither passes through the route layer. Known trade-off: redirects
    collapse into the original request, so page.url stays the pre-redirect
    URL; relative links are resolved by the extractor against the caller's
    URL, which matches that behavior.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "", ("dynamic rendering unavailable — install the optional "
                    "browser lane: pip install 'engawa-mcp[browser]' && "
                    "playwright install chromium")

    # Aggregate page budget. Admission is a reservation taken *before* any
    # await (handlers share one event loop, so check-and-reserve is atomic):
    # concurrent handlers cannot all pass a stale check and then each buffer
    # a full-size response. Unused reservation is refunded after the fetch.
    budget = {"left": AGGREGATE_MAX_BYTES}

    async def _guard_route(route):
        reserved = 0
        try:
            if route.request.method != "GET" or budget["left"] <= 0:
                await route.abort()
                return
            reserved = min(PAGE_MAX_BYTES, budget["left"])
            budget["left"] -= reserved
            fwd = {k: v for k, v in (route.request.headers or {}).items()
                   if k.lower() not in ("host", "content-length", "cookie")}
            resp, err = await net.guarded_get(route.request.url, timeout=20.0,
                                              headers=fwd,
                                              max_bytes=reserved,
                                              transport=_TRANSPORT_FOR_TESTS)
            if resp is None:
                budget["left"] += reserved  # full refund — nothing was kept
                reserved = 0
                await route.abort()
                return
            budget["left"] += reserved - len(resp.content)  # refund the unused part
            reserved = 0
            # set-cookie dropped: fulfill() takes a flat dict, so repeated
            # Set-Cookie fields would merge into one invalid value — and the
            # ephemeral context has no business keeping cookies anyway.
            await route.fulfill(status=resp.status_code,
                                headers={k: v for k, v in resp.headers.items()
                                         if k.lower() != "set-cookie"},
                                body=resp.content)
        except Exception:  # noqa: BLE001 — guard failure = don't fetch
            budget["left"] += reserved
            try:
                await route.abort()
            except Exception:  # noqa: BLE001
                pass

    async def _ws_block(ws_route):
        # Not calling connect_to_server() leaves the socket mocked — the
        # renderer sees a WebSocket that never reaches any real host.
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                ctx = await browser.new_context(
                    user_agent=BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 900},
                    service_workers="block",
                )
                await ctx.add_init_script(WEBRTC_KILL_SCRIPT)
                await ctx.route("**/*", _guard_route)
                await ctx.route_web_socket("**/*", _ws_block)
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    # Give SPA scripts a beat to fill the DOM; not mandatory.
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                return await page.content(), ""
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        return "", f"dynamic render failed: {type(e).__name__}: {str(e)[:120]}"


def extract(html: str, base_url: str) -> dict:
    """Pull title, article text, and outbound links from raw HTML."""
    title = ""
    content = ""
    try:
        doc = Document(html)
        title = (doc.title() or "").strip()
        article_soup = BeautifulSoup(doc.summary(), "html.parser")
        content = article_soup.get_text(separator="\n").strip()
        content = "\n".join(line.strip() for line in content.split("\n") if line.strip())
    except Exception:  # noqa: BLE001 — readability chokes on odd markup; fall through
        pass

    full_soup = BeautifulSoup(html, "html.parser")
    if not title and full_soup.title and full_soup.title.string:
        title = full_soup.title.string.strip()

    seen: set[str] = set()
    links: list[dict] = []
    for a in full_soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or len(text) < 2:
            continue
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        try:
            absolute = urljoin(base_url, href)
        except Exception:  # noqa: BLE001
            continue
        if not absolute.startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"text": text[:80], "url": absolute})
        if len(links) >= MAX_LINKS:
            break

    return {"title": title, "content": content, "links": links}


async def open_page(url: str, offset: int = 0, length: int = 3000) -> dict:
    """Fetch + extract one page. Static lane first, browser lane as fallback.

    Returns {title, content, total_length, links, note}. The URL is assumed
    to have passed the SSRF guard already (web_read does that).
    """
    html, static_err, renderable = await _fetch_static(url)
    parsed = extract(html, url) if html else None

    # parsed is None when the static lane produced no HTML. That normally
    # carries a static_err (a fetch/HTTP/content-type failure); the lone
    # exception — a 200 with an empty body — falls back to a generic message
    # below. The browser lane can still rescue a page whose static text was
    # merely thin; its failure never overwrites the first-hand static reason.
    needs_dynamic = renderable and (parsed is None
                                    or len(parsed["content"]) < SPA_CONTENT_THRESHOLD)
    if needs_dynamic:
        dyn_html, _dyn_err = await _fetch_dynamic(url)
        if dyn_html:
            parsed = extract(dyn_html, url)

    if parsed is None:
        return {"title": "", "content": "", "total_length": 0, "links": [],
                "note": static_err or "no readable content at that URL"}

    total = len(parsed["content"])
    sliced = parsed["content"][offset:offset + length] if length else parsed["content"][offset:]

    # Success receipt: stay quiet unless there's a real signal to flag. The
    # in-progress "trying browser render" chatter never belongs on a success.
    note = ""
    if total < 100:
        haystack = (parsed["title"] + parsed["content"]).lower()
        if any(kw in haystack for kw in ("log in", "sign in", "verify")):
            note = "the page may require a login"

    return {"title": parsed["title"], "content": sliced,
            "total_length": total, "links": parsed["links"], "note": note}
