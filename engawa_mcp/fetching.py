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

    Guard posture in this lane: a browser resolves DNS itself, so the HTTP
    lane's address pin does not apply here. Instead every request the page
    makes (navigation, redirects, subresources) passes through a per-host
    guard check and is aborted if it targets private address space. That is
    request filtering, not pinning — a documented residual of the optional
    browser lane.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "", ("dynamic rendering unavailable — install the optional "
                    "browser lane: pip install 'engawa-mcp[browser]' && "
                    "playwright install chromium")
    host_verdicts: dict[str, str] = {}

    async def _guard_route(route):
        try:
            host = httpx.URL(route.request.url).host or ""
            verdict = host_verdicts.get(host)
            if verdict is None:
                verdict = await net.url_public_ok(route.request.url)
                host_verdicts[host] = verdict
            if verdict:
                await route.abort()
            else:
                await route.continue_()
        except Exception:  # noqa: BLE001 — guard failure = don't fetch
            try:
                await route.abort()
            except Exception:  # noqa: BLE001
                pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 900},
                )
                page = await ctx.new_page()
                await page.route("**/*", _guard_route)
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
