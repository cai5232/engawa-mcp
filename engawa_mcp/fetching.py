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

# Test seam: injected into guarded_get so the static lane's redirect handling
# is testable with httpx.MockTransport. None in production.
_TRANSPORT_FOR_TESTS = None


async def _fetch_static(url: str) -> tuple[str, str]:
    """Fetch raw HTML through the guarded, DNS-pinned lane (net.guarded_get):
    every redirect hop is vetted before it is followed. Returns
    (html, error_note); empty html = failure."""
    try:
        resp, err = await net.guarded_get(url, timeout=20.0,
                                          headers=BROWSER_HEADERS,
                                          max_bytes=PAGE_MAX_BYTES,
                                          transport=_TRANSPORT_FOR_TESTS)
    except httpx.TimeoutException:
        return "", "static fetch timed out"
    except Exception as e:  # noqa: BLE001
        return "", f"static fetch failed: {type(e).__name__}"
    if resp is None:
        return "", err or "static fetch failed"
    if resp.status_code != 200:
        return "", f"HTTP {resp.status_code}"
    return resp.text, ""


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

    consumed = 0  # aggregate page budget — one hostile SPA can't stream forever

    async def _guard_route(route):
        nonlocal consumed
        try:
            if route.request.method != "GET" or consumed >= AGGREGATE_MAX_BYTES:
                await route.abort()
                return
            fwd = {k: v for k, v in (route.request.headers or {}).items()
                   if k.lower() not in ("host", "content-length", "cookie")}
            resp, err = await net.guarded_get(route.request.url, timeout=20.0,
                                              headers=fwd,
                                              max_bytes=PAGE_MAX_BYTES,
                                              transport=_TRANSPORT_FOR_TESTS)
            if resp is None:
                await route.abort()
                return
            consumed += len(resp.content)
            # set-cookie dropped: fulfill() takes a flat dict, so repeated
            # Set-Cookie fields would merge into one invalid value — and the
            # ephemeral context has no business keeping cookies anyway.
            await route.fulfill(status=resp.status_code,
                                headers={k: v for k, v in resp.headers.items()
                                         if k.lower() != "set-cookie"},
                                body=resp.content)
        except Exception:  # noqa: BLE001 — guard failure = don't fetch
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
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    # WebRTC can emit STUN over UDP without ever hitting the
                    # route layer — a direct network path around the guard.
                    # With no proxy configured, this policy silences it
                    # (verified: ICE gathering yields zero candidates).
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    # QUIC would let WebTransport talk UDP around the guard.
                    "--disable-quic",
                ],
            )
            try:
                ctx = await browser.new_context(
                    user_agent=BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 900},
                    service_workers="block",
                )
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
    html, static_err = await _fetch_static(url)
    parsed = extract(html, url) if html else None

    note_parts = []
    needs_dynamic = parsed is None or len(parsed["content"]) < SPA_CONTENT_THRESHOLD
    if needs_dynamic:
        note_parts.append(f"static: {static_err}" if not html else "thin static text, trying browser render")
        dyn_html, dyn_err = await _fetch_dynamic(url)
        if dyn_html:
            parsed = extract(dyn_html, url)
        elif parsed is None:
            note_parts.append(dyn_err)
            return {"title": "", "content": "", "total_length": 0, "links": [],
                    "note": " / ".join(p for p in note_parts if p) or "unreachable"}
        else:
            note_parts.append(dyn_err)

    total = len(parsed["content"])
    sliced = parsed["content"][offset:offset + length] if length else parsed["content"][offset:]

    note = " / ".join(p for p in note_parts if p)
    if total < 100 and not note:
        haystack = (parsed["title"] + parsed["content"]).lower()
        if any(kw in haystack for kw in ("log in", "sign in", "verify")):
            note = "the page may require a login"

    return {"title": parsed["title"], "content": sliced,
            "total_length": total, "links": parsed["links"], "note": note}
