"""engawa (縁側) — the porch of the house, the side that faces the world.

Your agent has tools for work. This is a porch for it to sit on: read a page
or a feed, check tonight's moon, look at one painting a day. Every upstream
is free and loginless; the only state on disk is a tiny cache. See README.

Tools (12):
    web_read, rss_read,
    shelf, shelf_add, shelf_suggest, shelf_remove,
    sky_tonight, apod, daily_art, arxiv_new, daily_poem, on_this_day
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date as date_cls
from datetime import datetime

import feedparser
import httpx
from mcp.server import MCPServer

from . import config, feeds, fetching, net, shelf, sky
from .feeds import err as _err

log = logging.getLogger("engawa")

mcp = MCPServer(
    name="engawa",
    instructions=("engawa: a porch for looking at the world — web pages, RSS, "
                  "a curated feed shelf, tonight's sky, and a few daily "
                  "delights. Public information only, no logins."),
)

# The dynamic-render fallback shares one Playwright profile; serialize to
# avoid cross-call profile lock contention. Low-concurrency use in mind.
_web_read_lock = asyncio.Lock()
_poem_token_lock = asyncio.Lock()


# ─── 1. web_read ─────────────────────────────────────────────────

@mcp.tool()
async def web_read(url: str, offset: int = 0, length: int = 3000) -> dict:
    """Read any web page: give a URL, get title + clean article text + links.

    Two lanes inside: httpx + readability for static pages (fast), and an
    automatic fallback to a real browser render for SPAs (needs the optional
    `engawa-mcp[browser]` extra). Long articles: page through with `offset`,
    watching `total_length`.

    Boundaries:
    - No login state, no history. To "click" a link, call web_read again
      with that link's URL.
    - Login-walled platforms (x.com etc.) won't yield anything useful.

    Args:
        url: full URL (http:// or https://)
        offset: character offset into the article text (for paging)
        length: max characters returned this call (default 3000)

    Returns:
        {ok, title, content, total_length, links (≤10), note}
    """
    def _fail(note: str) -> dict:
        return {"ok": False, "title": "", "content": "", "total_length": 0,
                "links": [], "note": note}

    if re.match(r"https?://(www\.|mobile\.)?(twitter\.com|x\.com)/", url or ""):
        return _fail("x.com sits behind a login wall; engawa reads the open web only")
    guard = await net.url_public_ok(url)
    if guard:
        return _fail(guard)
    offset = max(0, int(offset))
    length = max(200, min(int(length), 20000))
    async with _web_read_lock:
        page = await fetching.open_page(url, offset=offset, length=length)
    if page["total_length"] == 0 and not page["content"]:
        return _fail(page["note"] or "nothing readable at that URL")
    return {
        "ok": True,
        "title": page["title"],
        "content": page["content"],
        "total_length": page["total_length"],
        "links": page["links"][:10],
        "note": page["note"],
    }


# ─── 2. the shelf ────────────────────────────────────────────────

@mcp.tool(name="shelf")
async def shelf_list(tag: str = "") -> dict:
    """The engawa shelf: curated feed sources (name / one-liner / tags).
    Feed a name straight into rss_read.

    Two layers: built-in picks that ship with the package, plus whatever you
    add with shelf_add.

    Args:
        tag: filter to one shelf tag — science / ocean / longform /
             architecture / wonder. Empty = everything.

    Returns:
        {ok, tags, entries: [{name, title, url, tags, note, picked_by, source}], note}
    """
    tag = (tag or "").strip().lower()
    if tag and tag not in shelf.TAGS:
        return _err(f"unknown tag; choose from: {', '.join(shelf.TAGS)}",
                    tags=list(shelf.TAGS), entries=[])
    merged, merr = shelf.merged()
    entries = [shelf.entry_view(n, e) for n, e in merged.items()
               if not tag or tag in e.get("tags", [])]
    note = "call rss_read(feed_url=name) to read one" if entries else "this tag is still empty"
    if merr:
        note = f"⚠ {merr}. " + note
    return {"ok": True, "tags": list(shelf.TAGS), "entries": entries, "note": note}


@mcp.tool()
async def shelf_add(url: str, name: str = "", note: str = "", tags: str = "",
                    picked_by: str = "") -> dict:
    """Add a source to the shelf. Give any site address — if it is already a
    feed it is used as-is; otherwise its RSS/Atom feed is discovered
    (page-declared <link> first, common paths as fallback) and verified with
    a real fetch before anything is saved.

    Args:
        url: full http(s) address of a site or feed (e.g. https://nautil.us)
        name: shelf name (optional; lowercase letters/digits/hyphens, 2-40
              chars; empty = derived from the domain, collisions get -2)
        note: one-line description (optional)
        tags: comma-separated, from science/ocean/longform/architecture/wonder
        picked_by: free-form provenance — who asked for this source (optional)

    Returns:
        {ok, name, feed_title, feed_url, note}
    """
    name = (name or "").strip().lower()
    if name and not shelf.NAME_RE.match(name):
        return _err("name must be lowercase letters/digits/hyphens, 2-40 chars",
                    name=name, feed_title="", feed_url="")
    if name and name in shelf.BUILTIN:
        return _err(f"'{name}' is a built-in shelf name; pick another",
                    name=name, feed_title="", feed_url="")
    tag_list = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    bad = [t for t in tag_list if t not in shelf.TAGS]
    if bad:
        return _err(f"unknown tags {bad}; choose from: {', '.join(shelf.TAGS)}",
                    name=name, feed_title="", feed_url="")
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url          # accept bare "nautil.us" too
    host = (httpx.URL(url).host or "") if re.match(r"^https?://", url, re.I) else ""
    if not host or ("." not in host and host != "localhost"):
        # A dotless "host" is almost always a shelf name passed by mistake.
        return _err("url must be a full site address (like https://nautil.us), "
                    "not a shelf name", name=name, feed_title="", feed_url="")
    feed_url, probe, derr = await shelf.discover_feed(url)
    if not feed_url or probe is None:
        return _err(f"not added: {derr}", name=name, feed_title="", feed_url="")
    async with shelf.lock:
        user, lerr = shelf.user_load()
        if lerr:  # refuse writes while the file is unhealthy — saving would drop records
            return _err(f"not added: {lerr}", name=name, feed_title="", feed_url=feed_url)
        # Same-feed dedup regardless of layer or whether a name was given.
        nf = shelf.norm_feed_url(feed_url)
        hit_builtin = next((k for k, v in shelf.BUILTIN.items()
                            if shelf.norm_feed_url(v["url"]) == nf), None)
        if hit_builtin:
            return _err(f"already on the built-in shelf as '{hit_builtin}'",
                        name=hit_builtin, feed_title=probe.get("feed_title", ""),
                        feed_url=feed_url)
        same_user = next((k for k, v in user.items()
                          if shelf.norm_feed_url(v.get("url", "")) == nf), None)
        if same_user and name and name != same_user:
            return _err(f"already on the shelf as '{same_user}'; use that name to update",
                        name=same_user, feed_title=probe.get("feed_title", ""),
                        feed_url=feed_url)
        if not name:
            # Derive from the address the caller gave, not the feed host — a
            # site served through FeedBurner should not be named "feedburner".
            name = same_user or shelf.auto_name(url, set(user) | set(shelf.BUILTIN))
        existed = name in user
        user[name] = {"title": probe.get("feed_title") or name, "url": feed_url,
                      "tags": tag_list, "note": (note or "")[:200],
                      "picked_by": re.sub(r"\s+", " ", str(picked_by or "")).strip()[:shelf.PICKED_BY_MAX]}
        shelf.user_save(user)
    return {"ok": True, "name": name, "feed_title": probe.get("feed_title", ""),
            "feed_url": feed_url, "note": "updated" if existed else "added"}


@mcp.tool()
async def shelf_suggest(tag: str = "") -> dict:
    """A second ring of verified sources that are not on the default shelf.
    Like any of them? shelf_add(url=its url, name=its name).

    Args:
        tag: filter to one shelf tag, empty = everything

    Returns:
        {ok, tags, entries: [{name, title, url, tags, note, on_shelf}], note}
        on_shelf=True means it is already on your shelf (same name or url).
    """
    tag = (tag or "").strip().lower()
    if tag and tag not in shelf.TAGS:
        return _err(f"unknown tag; choose from: {', '.join(shelf.TAGS)}",
                    tags=list(shelf.TAGS), entries=[])
    merged, _merr = shelf.merged()
    on_names = set(merged)
    on_urls = {e.get("url") for e in merged.values()}
    entries = []
    for n, e in shelf.SUGGEST.items():
        if tag and tag not in e["tags"]:
            continue
        entries.append({"name": n, "title": e["title"], "url": e["url"],
                        "tags": list(e["tags"]), "note": e["note"],
                        "on_shelf": (n in on_names or e["url"] in on_urls)})
    return {"ok": True, "tags": list(shelf.TAGS), "entries": entries,
            "note": ("like one? shelf_add(url=its url, name=its name)"
                     if entries else "no suggestions under this tag")}


@mcp.tool()
async def shelf_remove(name: str) -> dict:
    """Remove a user-added source from the shelf (built-ins cannot be removed).

    Args:
        name: shelf name

    Returns:
        {ok, name, note}
    """
    name = (name or "").strip().lower()
    async with shelf.lock:
        user, lerr = shelf.user_load()
        if lerr:  # same rule as shelf_add: no writes while the file is unhealthy
            return _err(f"not removed: {lerr}", name=name)
        if name in user:  # user layer first: shadowed hand-edited records stay removable
            del user[name]
            shelf.user_save(user)
            return {"ok": True, "name": name, "note": "removed"}
    if name in shelf.BUILTIN:
        return _err(f"'{name}' is built-in and cannot be removed", name=name)
    return _err(f"nothing on the shelf named '{name}'", name=name)


# ─── 3. rss_read ─────────────────────────────────────────────────

@mcp.tool()
async def rss_read(feed_url: str, limit: int = 10) -> dict:
    """Read any RSS/Atom feed: latest entries as a list.

    The discovery half of a browse loop: spot an interesting entry, then
    web_read its link for the full text. Also accepts shelf names (see the
    shelf tool), e.g. rss_read("aeon").

    Args:
        feed_url: full feed URL, or a shelf name
        limit: max entries returned (default 10)

    Returns:
        {ok, feed_title, entries: [{title, link, published, summary}], note}
    """
    feed_url, rerr = shelf.resolve(feed_url)
    if rerr:
        return _err(rerr, feed_title="", entries=[])
    return await feeds.read_feed(feed_url, limit=limit)


# ─── 4. sky_tonight ──────────────────────────────────────────────

@mcp.tool()
async def sky_tonight(date: str = "") -> dict:
    """Tonight's sky: moon phase, illumination, moonrise/set, sunset, next
    full/new moon, and which naked-eye planets are up at 21:00.

    Computed locally (set ENGAWA_LAT / ENGAWA_LON for your location) — the
    one tool here that never touches the network.

    Args:
        date: "YYYY-MM-DD"; empty = today (local timezone)

    Returns:
        {ok, date, moon_phase, moon_illumination, moonrise, moonset, sunset,
         next_full_moon, next_new_moon, planets_2100}
    """
    try:
        day = (date_cls.fromisoformat(date) if date
               else datetime.now(config.local_tz()).date())
    except ValueError:
        return _err(f"date must be YYYY-MM-DD, got: {date!r}")
    return {"ok": True, **sky.compute(day)}


# ─── 5. apod ─────────────────────────────────────────────────────

@mcp.tool()
async def apod(date: str = "") -> dict:
    """NASA Astronomy Picture of the Day: title, an astronomer's explanation,
    and the image URL.

    Args:
        date: "YYYY-MM-DD"; empty = today. NASA publishes on US Eastern time,
              so "today" is occasionally not up yet — if you get a 404 note,
              call again with yesterday's date.

    Returns:
        {ok, title, date, explanation, url, hdurl, media_type, note}
    """
    params = {"api_key": config.NASA_API_KEY}
    if date:
        try:
            date_cls.fromisoformat(date)
        except ValueError:
            return _err(f"date must be YYYY-MM-DD, got: {date!r}")
        params["date"] = date
    # This endpoint is famously slow; give it extra rope.
    data, gerr = await net.get_json("https://api.nasa.gov/planetary/apod",
                                    params=params, timeout=30.0)
    if data is None:
        return _err(f"NASA APOD unreachable: {gerr}")
    return {
        "ok": True,
        "title": data.get("title", ""),
        "date": data.get("date", ""),
        "explanation": data.get("explanation", "")[:1200],
        "url": data.get("url", ""),
        "hdurl": data.get("hdurl", ""),
        "media_type": data.get("media_type", ""),
        "note": "" if config.NASA_API_KEY != "DEMO_KEY"
                else "using DEMO_KEY (≈50 req/day); free personal keys at api.nasa.gov",
    }


# ─── 6. daily_art ────────────────────────────────────────────────

_AIC_FIELDS = "id,title,artist_display,date_display,medium_display,image_id"


def _aic_pick_index(day: date_cls, page_size: int, page_count: int) -> tuple[int, int]:
    """Deterministic (page, index) from the date — everyone who asks on the
    same day sees the same painting. That is what makes it 'daily'."""
    seed = day.toordinal()
    return (seed * 31 % page_count) + 1, seed * 7 % page_size


@mcp.tool()
async def daily_art(keyword: str = "") -> dict:
    """One artwork a day from the Art Institute of Chicago's public-domain
    collection (no API key needed).

    Without a keyword: the piece of the day (date-seeded, so repeat calls on
    the same day return the same artwork). With a keyword: search instead
    (English terms hit best — "monet", "cat", "moon").

    Returns:
        {ok, title, artist, date, medium, image_url, page_url, note}
    """
    base = "https://api.artic.edu/api/v1/artworks/search"
    filters = [
        ("query[bool][must][][term][is_public_domain]", "true"),
        ("query[bool][must][][exists][field]", "image_id"),
        ("fields", _AIC_FIELDS),
    ]
    page_size = 10
    if keyword:
        params = filters + [("q", keyword), ("limit", str(page_size))]
    else:
        page, _ = _aic_pick_index(datetime.now(config.local_tz()).date(), page_size, 90)
        params = filters + [("limit", str(page_size)), ("page", str(page))]
    data, gerr = await net.get_json(base, params=params, timeout=20.0)
    if data is None:
        return _err(f"Art Institute of Chicago API unreachable: {gerr}")
    arts = [a for a in data.get("data", []) if a.get("image_id")]
    if not arts:
        return _err("no public-domain artworks with images found"
                    + (f" (keyword={keyword!r})" if keyword else ""))
    if keyword:
        art = arts[0]
    else:
        _, idx = _aic_pick_index(datetime.now(config.local_tz()).date(), len(arts), 90)
        art = arts[idx % len(arts)]
    return {
        "ok": True,
        "title": art.get("title", ""),
        "artist": art.get("artist_display", ""),
        "date": art.get("date_display", ""),
        "medium": art.get("medium_display", ""),
        "image_url": f"https://www.artic.edu/iiif/2/{art['image_id']}/full/843,/0/default.jpg",
        "page_url": f"https://www.artic.edu/artworks/{art.get('id', '')}",
        "note": "",
    }


# ─── 7. arxiv_new ────────────────────────────────────────────────

@mcp.tool()
async def arxiv_new(category: str = "cs.AI", limit: int = 8) -> dict:
    """Newest arXiv papers in a category: title + authors + abstract.

    Args:
        category: arXiv category, e.g. cs.AI / cs.CL / astro-ph.EP / math.HO
        limit: max papers returned (default 8)

    Returns:
        {ok, category, papers: [{title, authors, summary, link, published}], note}
    """
    if not re.fullmatch(r"[a-z-]+(\.[A-Za-z-]+)?", category):
        return _err(f"malformed category: {category!r} (examples: cs.AI / astro-ph.EP)",
                    papers=[])
    params = {
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": str(max(1, min(limit, 20))),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get("https://export.arxiv.org/api/query",
                                    params=params, headers=net.UA)
        if resp.status_code != 200:
            return _err(f"arXiv HTTP {resp.status_code}", papers=[])
        parsed = feedparser.parse(resp.content)
    except httpx.TimeoutException:
        return _err("arXiv timed out", papers=[])
    except Exception as e:  # noqa: BLE001
        return _err(f"arXiv failed: {type(e).__name__}", papers=[])
    papers = []
    for e in parsed.entries:
        authors = [a.get("name", "") for a in e.get("authors", [])]
        papers.append({
            "title": net.strip_html(e.get("title", ""), 250),
            "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
            "summary": net.strip_html(e.get("summary", ""), 400),
            "link": e.get("link", ""),
            "published": e.get("published", "")[:10],
        })
    if not papers:
        return _err(f"zero results for category {category} (typo in the category?)",
                    papers=[])
    return {"ok": True, "category": category, "papers": papers, "note": ""}


# ─── 8. daily_poem ───────────────────────────────────────────────

def _poem_token_path():
    return config.CACHE_DIR / "jinrishici_token.json"


async def _poem_token(client: httpx.AsyncClient) -> tuple[str, str]:
    """Jinrishici token: an anonymous, registration-free rate-limit ticket
    (not an account). Fetched on first use, cached on disk. A failed cache
    write only logs — next call re-fetches, harmless. Returns (token, err)."""
    async with _poem_token_lock:
        p = _poem_token_path()
        try:
            cached = json.loads(p.read_text())
            if cached.get("token"):
                return cached["token"], ""
        except (OSError, ValueError):
            pass
        try:
            resp = await client.get("https://v2.jinrishici.com/token", headers=net.UA)
            token = resp.json().get("data", "")
        except Exception as e:  # noqa: BLE001
            return "", f"token fetch failed: {type(e).__name__}"
        if not isinstance(token, str) or not token:
            return "", "token fetch failed: empty response"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"token": token}))
            os.replace(tmp, p)  # atomic; no half-written files
        except OSError as e:
            log.warning("poem token cache write failed (will re-fetch next time): %s", e)
        return token, ""


@mcp.tool()
async def daily_poem() -> dict:
    """A line of classical Chinese poetry matched to the season and hour, with
    its source poem in full. Powered by the Jinrishici (今日诗词) API; content
    is in Chinese — that is the point, not a bug.

    Returns:
        {ok, sentence, title, dynasty, author, full, note}
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            token, terr = await _poem_token(client)
            if not token:
                return _err(f"jinrishici: {terr}")
            resp = await client.get("https://v2.jinrishici.com/sentence",
                                    headers={**net.UA, "X-User-Token": token})
            body = resp.json()
    except httpx.TimeoutException:
        return _err("jinrishici timed out")
    except Exception as e:  # noqa: BLE001
        return _err(f"jinrishici failed: {type(e).__name__}")
    if body.get("status") != "success":
        return _err(f"jinrishici returned an error: {str(body)[:120]}")
    data = body.get("data", {})
    origin = data.get("origin", {})
    return {
        "ok": True,
        "sentence": data.get("content", ""),
        "title": origin.get("title", ""),
        "dynasty": origin.get("dynasty", ""),
        "author": origin.get("author", ""),
        "full": "\n".join(origin.get("content", []) or []),
        "note": "",
    }


# ─── 9. on_this_day ──────────────────────────────────────────────

@mcp.tool()
async def on_this_day(lang: str = "en", limit: int = 8) -> dict:
    """Wikipedia's curated "on this day" events.

    Note: this Wikipedia API serves a subset of languages (en/de/fr/es/pt/ru
    and a few more); unsupported languages 404.

    Args:
        lang: Wikipedia language code (default en)
        limit: max events returned (default 8)

    Returns:
        {ok, date, events: [{year, text}], note}
    """
    if not re.fullmatch(r"[a-z]{2,8}", lang):
        return _err(f"malformed language code: {lang!r}", events=[])
    now = datetime.now(config.local_tz())
    url = (f"https://{lang}.wikipedia.org/api/rest_v1/feed/onthisday/selected"
           f"/{now.month:02d}/{now.day:02d}")
    data, gerr = await net.get_json(url, timeout=15.0)
    if data is None:
        note = f"Wikipedia ({lang}) unreachable: {gerr}"
        if gerr == "HTTP 404":
            note += " — this language may not support the API; try lang='en'"
        return _err(note, events=[])
    events = [{"year": e.get("year"), "text": e.get("text", "")}
              for e in data.get("selected", [])[: max(1, min(limit, 20))]]
    return {"ok": True, "date": now.strftime("%m-%d"), "events": events, "note": ""}
