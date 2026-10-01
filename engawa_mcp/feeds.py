"""RSS/Atom reading: guarded fetch, parse, validity check.

Used by the rss_read tool and by shelf feed discovery (which probes candidate
URLs with the exact same reader, so nothing lands on the shelf that rss_read
cannot actually read).
"""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx

from . import net


def err(note: str, **extra) -> dict:
    """Uniform failure shape: the note says which upstream failed and why.
    Fail loud — a silent empty result teaches the calling agent wrong rules."""
    return {"ok": False, "note": note, **extra}


# Refuse to parse absurdly large "feeds" — a real feed is a few hundred KB at
# most; anything bigger is either broken or hostile.
FEED_MAX_BYTES = 10 * 1024 * 1024


BING_HOSTS = {"bing.com", "www.bing.com", "cn.bing.com"}


def unwrap_link(link: str) -> str:
    """Bing News feed links are click-tracking redirects
    (bing.com/news/apiclick.aspx?...&url=<publisher url>). Return the
    publisher's own address so web_read reads the article, not the tracker.
    Anything else — or a wrapper whose target is not http(s) — passes through
    unchanged. The unwrapped URL is still subject to the SSRF guard when read."""
    try:
        u = urlparse(link)
    except ValueError:
        return link
    if (u.hostname or "").lower() in BING_HOSTS and u.path.lower() == "/news/apiclick.aspx":
        target = (parse_qs(u.query).get("url") or [""])[0].strip()
        if target.lower().startswith(("http://", "https://")):
            return target
    return link


async def read_feed(feed_url: str, limit: int = 10) -> dict:
    """Fetch and parse one feed URL. Returns
    {ok, feed_title, entries: [{title, link, published, summary}], note}.
    """
    guard = await net.url_public_ok(feed_url)
    if guard:
        return err(guard, feed_title="", entries=[])
    resp = None
    fail = ""
    for attempt in (0, 1):  # one retry on transient 5xx / network blips
        if attempt:
            await asyncio.sleep(1.5)
        try:
            resp, fail = await net.guarded_get(feed_url, timeout=15.0,
                                               max_bytes=FEED_MAX_BYTES)
            if resp is None:
                # Redirect-chain rejections are deterministic; don't retry.
                return err(fail or "feed fetch failed", feed_title="", entries=[])
            if resp.status_code == 200:
                fail = ""
                break
            fail = f"feed returned HTTP {resp.status_code}"
            if resp.status_code < 500:
                return err(fail, feed_title="", entries=[])
            resp = None  # 5xx: clear and retry once
        except httpx.TimeoutException:
            fail = "feed timed out"
        except httpx.TransportError:
            fail = "feed connection failed"
        except Exception as e:  # noqa: BLE001
            return err(f"feed fetch failed: {type(e).__name__}", feed_title="", entries=[])
    if fail or resp is None:
        return err(fail or "feed fetch failed", feed_title="", entries=[])
    if len(resp.content) > FEED_MAX_BYTES:
        return err(f"feed too large (> {FEED_MAX_BYTES // (1024 * 1024)} MB)",
                   feed_title="", entries=[])
    try:
        # feedparser is synchronous; run it off the event loop so callers'
        # timeouts (e.g. shelf discovery's budget) can actually fire.
        parsed = await asyncio.to_thread(feedparser.parse, resp.content)
    except Exception as e:  # noqa: BLE001
        return err(f"feed parse failed: {type(e).__name__}", feed_title="", entries=[])

    # feedparser happily swallows plain HTML; a real feed always has a version
    # (rss20/atom10/…). No version and no entries ⇒ this is not a feed.
    if not parsed.version and not parsed.entries:
        return err("not a valid RSS/Atom feed (the URL may be an ordinary web "
                   "page; feed addresses usually hide behind the RSS icon or a "
                   "<link rel=\"alternate\"> tag)", feed_title="", entries=[])
    entries = []
    for e in parsed.entries[: max(1, min(limit, 30))]:
        entries.append({
            "title": net.strip_html(e.get("title", ""), 200),
            "link": unwrap_link(e.get("link", "")),
            "published": e.get("published", e.get("updated", "")),
            "summary": net.strip_html(e.get("summary", ""), 400),
        })
    return {
        "ok": True,
        "feed_title": net.strip_html(parsed.feed.get("title", ""), 200),
        "entries": entries,
        "note": "",
    }
