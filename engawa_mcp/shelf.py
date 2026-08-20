"""The engawa shelf: a curated feed directory in two layers.

Built-in layer: hand-picked sources, every one verified alive with this
package's own reader before shipping. User layer: whatever you add with
shelf_add, persisted to CACHE_DIR/shelf.json (atomic writes; a corrupt file
is treated as empty and reported, never silently overwritten).

Names, not URLs, are the interface: `rss_read("aeon")` just works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config, feeds

log = logging.getLogger("engawa.shelf")

TAGS = ("science", "ocean", "longform", "architecture", "wonder")

# All entries verified 2026-08-19/20: fetched with this package's UA, parsed
# as a valid feed, latest entry reasonably fresh. `picked_by` ships empty —
# it is a free-form provenance field for your own household to fill.
BUILTIN: dict[str, dict] = {
    # science — trivia and wonder, not paper-grinding
    "nautilus": {"title": "Nautilus", "url": "https://nautil.us/feed/",
                 "tags": ["science", "longform"], "picked_by": "",
                 "note": "science longform that reads like stories"},
    "sciencealert": {"title": "ScienceAlert", "url": "https://www.sciencealert.com/feed",
                     "tags": ["science"], "picked_by": "",
                     "note": "short science news, high trivia density"},
    "livescience": {"title": "Live Science", "url": "https://www.livescience.com/feeds/all",
                    "tags": ["science"], "picked_by": "",
                    "note": "quick reads on animals, space, and the human body"},
    "smithsonian-science": {"title": "Smithsonian Magazine · Science",
                            "url": "https://www.smithsonianmag.com/rss/science-nature/",
                            "tags": ["science", "wonder"], "picked_by": "",
                            "note": "the Smithsonian's science & nature desk"},
    "atlas-obscura": {"title": "Atlas Obscura", "url": "https://www.atlasobscura.com/feeds/latest",
                      "tags": ["wonder"], "picked_by": "",
                      "note": "curious places and stories from around the world"},
    "quanta": {"title": "Quanta Magazine", "url": "https://api.quantamagazine.org/feed/",
               "tags": ["science", "longform"], "picked_by": "",
               "note": "frontier math/physics/biology longform — demanding but rewarding"},
    # ocean
    "ocean-conservancy": {"title": "Ocean Conservancy", "url": "https://oceanconservancy.org/feed/",
                          "tags": ["ocean"], "picked_by": "",
                          "note": "blog of the ocean conservation nonprofit"},
    "mbari": {"title": "MBARI", "url": "https://www.mbari.org/feed/",
              "tags": ["ocean", "science"], "picked_by": "",
              "note": "Monterey Bay Aquarium Research Institute — deep-sea news firsthand"},
    "biographic": {"title": "bioGraphic", "url": "https://www.biographic.com/feed/",
                   "tags": ["ocean", "longform", "wonder"], "picked_by": "",
                   "note": "nature storytelling from the California Academy of Sciences, superb photography"},
    "schmidt-ocean": {"title": "Schmidt Ocean Institute", "url": "https://schmidtocean.org/feed/",
                      "tags": ["ocean"], "picked_by": "",
                      "note": "expeditions and discoveries from a deep-sea research vessel"},
    "noaa": {"title": "NOAA", "url": "https://www.noaa.gov/rss.xml",
             "tags": ["ocean", "science"], "picked_by": "",
             "note": "news from the US National Oceanic and Atmospheric Administration"},
    # longform — things someone sat down and really wrote
    "aeon": {"title": "Aeon", "url": "https://aeon.co/feed.rss",
             "tags": ["longform"], "picked_by": "",
             "note": "essays on consciousness, time, and questions worth sitting with"},
    "marginalian": {"title": "The Marginalian", "url": "https://www.themarginalian.org/feed/",
                    "tags": ["longform"], "picked_by": "",
                    "note": "Maria Popova's decades-long reading-and-life notebook"},
    "longreads": {"title": "Longreads", "url": "https://longreads.com/feed/",
                  "tags": ["longform"], "picked_by": "",
                  "note": "curated longform from across the web"},
    # architecture — why a building is the way it is
    "architectural-review": {"title": "The Architectural Review",
                             "url": "https://www.architectural-review.com/feed",
                             "tags": ["architecture", "longform"], "picked_by": "",
                             "note": "architecture criticism, not real-estate listing"},
    "common-edge": {"title": "Common Edge", "url": "https://commonedge.org/feed/",
                    "tags": ["architecture"], "picked_by": "",
                    "note": "architecture and its relationship to people, essay-leaning"},
    "99pi": {"title": "99% Invisible", "url": "https://99percentinvisible.org/feed/",
             "tags": ["architecture", "wonder"], "picked_by": "",
             "note": "the unnoticed design behind everyday things"},
    "archdaily": {"title": "ArchDaily", "url": "https://www.archdaily.com/feed",
                  "tags": ["architecture"], "picked_by": "",
                  "note": "daily architecture projects, image-heavy"},
    "dezeen": {"title": "Dezeen", "url": "https://www.dezeen.com/feed/",
               "tags": ["architecture"], "picked_by": "",
               "note": "architecture and design news"},
}

# A second ring of verified sources that did not make the default shelf —
# shelf_suggest serves these so "what else is out there?" has an answer that
# is one shelf_add away instead of a search engine away.
SUGGEST: dict[str, dict] = {
    # science
    "scientific-american": {"title": "Scientific American",
                            "url": "https://www.scientificamerican.com/platform/syndication/rss/",
                            "tags": ["science"], "note": "the venerable generalist — covers everything, clearly"},
    "nature-news": {"title": "Nature · News", "url": "https://www.nature.com/nature.rss",
                    "tags": ["science"], "note": "Nature's news face: frontier without the paper-grinding"},
    "new-scientist": {"title": "New Scientist", "url": "https://www.newscientist.com/feed/home/",
                      "tags": ["science"], "note": "fast, idea-dense UK science weekly"},
    "ars-technica-science": {"title": "Ars Technica · Science",
                             "url": "https://arstechnica.com/science/feed/",
                             "tags": ["science"], "note": "deep, hard science-and-tech reporting"},
    "undark": {"title": "Undark", "url": "https://undark.org/feed/",
               "tags": ["science", "longform"], "note": "deep reporting where science meets society"},
    "bigthink": {"title": "Big Think", "url": "https://bigthink.com/feed/",
                 "tags": ["science", "wonder"], "note": "science, philosophy, odd questions — short and tasty"},
    # ocean
    "oceana": {"title": "Oceana", "url": "https://oceana.org/feed/",
               "tags": ["ocean"], "note": "ocean conservation group — what's happening in the water"},
    "whoi": {"title": "Woods Hole Oceanographic Institution", "url": "https://www.whoi.edu/feed/",
             "tags": ["ocean", "science"], "note": "deep-sea expeditions, firsthand"},
    "mongabay": {"title": "Mongabay", "url": "https://news.mongabay.com/feed/",
                 "tags": ["ocean", "science"], "note": "conservation news (rainforests and wildlife too, not just sea)"},
    # longform
    "psyche": {"title": "Psyche", "url": "https://psyche.co/feed",
               "tags": ["longform"], "note": "Aeon's sister magazine, on the mind and how to live"},
    "paris-review": {"title": "The Paris Review · Daily", "url": "https://www.theparisreview.org/blog/feed/",
                     "tags": ["longform"], "note": "the Paris Review's daily — literature and marginalia"},
    "the-point": {"title": "The Point", "url": "https://thepointmag.com/feed/",
                  "tags": ["longform"], "note": "a magazine of ideas from Chicago, slow and deep"},
    "laphams": {"title": "Lapham's Quarterly", "url": "https://www.laphamsquarterly.org/rss.xml",
                "tags": ["longform", "wonder"], "note": "essays that hold history up to the present"},
    # architecture
    "architizer": {"title": "Architizer Journal", "url": "https://architizer.com/blog/feed/",
                   "tags": ["architecture"], "note": "projects and design essays, image-heavy"},
    "designboom-arch": {"title": "designboom · architecture",
                        "url": "https://www.designboom.com/architecture/feed/",
                        "tags": ["architecture"], "note": "a few new buildings every day, short and quick"},
    "curbed": {"title": "Curbed", "url": "https://www.curbed.com/rss/index.xml",
               "tags": ["architecture", "wonder"], "note": "New York's city-and-living observer"},
    # wonder
    "public-domain-review": {"title": "The Public Domain Review",
                             "url": "https://publicdomainreview.org/rss.xml",
                             "tags": ["wonder", "longform"], "note": "strange corners of old books and old pictures"},
    "futility-closet": {"title": "Futility Closet", "url": "https://www.futilitycloset.com/feed/",
                        "tags": ["wonder"], "note": "one odd little anecdote a day"},
    "kottke": {"title": "kottke.org", "url": "https://feeds.kottke.org/main",
               "tags": ["wonder"], "note": "the web's longest-running curiosity blog"},
    "open-culture": {"title": "Open Culture", "url": "https://www.openculture.com/feed",
                     "tags": ["wonder", "longform"], "note": "free courses, old films, lost recordings"},
    "amusing-planet": {"title": "Amusing Planet", "url": "https://www.amusingplanet.com/feeds/posts/default",
                       "tags": ["wonder"], "note": "remarkable places and objects around the world"},
}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
PICKED_BY_MAX = 24

lock = asyncio.Lock()


def _user_path() -> Path:
    return config.CACHE_DIR / "shelf.json"


def entry_valid(k, v) -> dict | None:
    """Strict validation of one user-layer record: name well-formed, url
    http(s), tags a list of known strings, text fields strings-or-absent.
    Anything off ⇒ None (a hand-edited `tags: null` must not crash list())."""
    if not (isinstance(k, str) and NAME_RE.match(k) and isinstance(v, dict)):
        return None
    url = v.get("url")
    if not (isinstance(url, str) and re.match(r"^https?://", url, re.I)):
        return None
    tags = v.get("tags", [])
    if not (isinstance(tags, list) and all(isinstance(t, str) and t in TAGS for t in tags)):
        return None
    out = {"url": url, "tags": list(tags)}
    for f in ("title", "note", "picked_by"):
        val = v.get(f, "")
        if not isinstance(val, str):
            return None
        out[f] = val
    return out


def user_load() -> tuple[dict[str, dict], str]:
    """Load the user layer. Returns (entries, err). A corrupt file or invalid
    records never crash the server, but the error must surface in tool
    receipts — and while it stands, writes are refused (saving would silently
    drop the skipped records)."""
    p = _user_path()
    if not p.exists():
        return {}, ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to parse %s: %s", p, type(e).__name__)
        return {}, (f"user shelf.json is corrupt ({type(e).__name__}); treated as "
                    "empty — fix or delete it before adding/removing")
    if not isinstance(data, dict):
        return {}, "user shelf.json top level is not an object; treated as empty"
    good: dict[str, dict] = {}
    skipped = []
    for k, v in data.items():
        entry = entry_valid(k, v)
        if entry is None:
            skipped.append(str(k))
        else:
            good[k] = entry
    err = (f"skipped {len(skipped)} invalid user records: {skipped[:5]} "
           "(fix or remove them before adding/removing)" if skipped else "")
    return good, err


def user_save(data: dict[str, dict]) -> None:
    """Atomic within one process (unique temp file + rename). Two server
    processes sharing one ENGAWA_CACHE_DIR can still lose one of two
    concurrent updates — run one instance per cache dir."""
    p = _user_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".shelf-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)


def merged() -> tuple[dict[str, dict], str]:
    """Built-ins first, user layer second. shelf_add refuses name collisions,
    but a hand-edited file can collide — built-in wins and the shadowed record
    is reported (shelf_remove can still delete it)."""
    out = {k: {**v, "source": "builtin"} for k, v in BUILTIN.items()}
    user, err = user_load()
    shadowed = []
    for k, v in user.items():
        if k in out:
            shadowed.append(k)
            continue
        out[k] = {**v, "source": "user"}
    if shadowed:
        err = (err + "; " if err else "") + (
            f"user records shadowed by built-ins of the same name: {shadowed} "
            "(shelf_remove can delete them)")
    return out, err


def entry_view(name: str, e: dict) -> dict:
    return {"name": name, "title": e.get("title", name), "url": e.get("url", ""),
            "tags": list(e.get("tags", [])), "note": e.get("note", ""),
            "picked_by": e.get("picked_by", ""), "source": e.get("source", "")}


def resolve(feed_url: str) -> tuple[str, str]:
    """Shelf name → URL; a URL passes through unchanged. Returns (url, err)."""
    key = (feed_url or "").strip()
    if re.match(r"^https?://", key, re.I):
        return key, ""
    entry = merged()[0].get(key.lower())
    if entry:
        return entry["url"], ""
    return "", (f"'{key}' is neither a URL nor a shelf name; see shelf() for names"
                if key else "feed_url is empty")


def norm_feed_url(u: str) -> str:
    """Normalization for de-duplication only (never stored): lowercase
    scheme/host, strip trailing slash and fragment."""
    try:
        x = httpx.URL((u or "").strip())
        path = (x.path or "/").rstrip("/") or "/"
        q = f"?{x.query.decode()}" if x.query else ""
        return f"{x.scheme.lower()}://{x.host.lower()}{(':' + str(x.port)) if x.port else ''}{path}{q}"
    except Exception:  # noqa: BLE001
        return (u or "").strip().rstrip("/")


_CC_SECOND = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}


def auto_name(feed_url: str, taken: set[str]) -> str:
    """Derive a shelf name from the domain: drop www./feeds./rss. prefixes,
    take the registrable label (country second-level suffixes like .co.uk are
    stripped as two labels), slugify; all-non-ASCII degrades to 'site';
    collisions get -2/-3; total length ≤ 40."""
    host = (urlparse(feed_url).hostname or "site").lower()
    host = re.sub(r"^(www|feeds?|rss|blog|api)\.", "", host)
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _CC_SECOND:
        parts = parts[:-2] + [parts[-1]]          # bbci.co.uk → bbci.uk → bbci
    base = "-".join(parts[:-1]) if len(parts) >= 2 else (parts[0] if parts else "site")
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:36] or "site"
    if len(base) < 2:
        base = (base + "-feed")[:40]
    name, i = base, 2
    while name in taken:
        name = f"{base[:36]}-{i}"
        i += 1
    return name


# ── feed discovery: any site URL → its feed ─────────────────────────────

DISCOVER_HTML_MAX = 2 * 1024 * 1024
# Common feed locations when the page declares no <link rel=alternate>.
GUESS_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
               "/index.xml", "/feeds/all", "/rss/", "/blog/feed", "/?feed=rss2")
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))')

DISCOVER_MAX_TRY = 5      # declared + guessed candidates probed, total
DISCOVER_BUDGET_S = 45.0  # hard wall-clock budget for one discovery


def html_feed_links(html: str, base: str) -> list[str]:
    """Collect feed URLs from <link rel=alternate type=application/(rss|atom)+xml>
    tags (relative hrefs resolved against base; order-preserving dedup).
    Regex, not a full parse: we only want link tags and the page may be cut off."""
    out: list[str] = []
    for tag in _LINK_TAG_RE.findall(html[:DISCOVER_HTML_MAX]):
        attrs = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "")
                 for m in _ATTR_RE.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        typ = attrs.get("type", "").lower()
        href = attrs.get("href", "").strip()
        if "alternate" in rel and href and ("rss" in typ or "atom" in typ):
            try:
                full = str(httpx.URL(base).join(href))
            except Exception:  # noqa: BLE001
                continue
            if full not in out:
                out.append(full)
    return out


async def discover_feed(site_url: str, max_try: int = DISCOVER_MAX_TRY) -> tuple[str, dict | None, str]:
    """Any site URL → (feed_url, probe receipt, err).
    Order: ① the URL is already a feed ② <link rel=alternate> declarations
    ③ common-path guesses. Every fetch goes through the SSRF guard. Budgeted:
    at most max_try candidates and DISCOVER_BUDGET_S seconds overall — one
    slow site must not stall the tool for minutes."""
    from . import net  # local import to keep patch points obvious in tests
    try:
        async with asyncio.timeout(DISCOVER_BUDGET_S):
            return await _discover_inner(site_url, max_try, net)
    except TimeoutError:
        return "", None, (f"feed discovery exceeded {int(DISCOVER_BUDGET_S)}s; "
                          "the site is too slow, skipping")


async def _discover_inner(site_url: str, max_try: int, net) -> tuple[str, dict | None, str]:
    probe = await feeds.read_feed(site_url, limit=1)
    if probe.get("ok"):
        return site_url, probe, ""
    cands: list[str] = []
    try:
        resp, err = await net.guarded_get(site_url, timeout=15.0,
                                          max_bytes=DISCOVER_HTML_MAX)
    except (httpx.TimeoutException, httpx.TransportError):
        resp, err = None, "site unreachable (timeout / connection failed)"
    except Exception as e:  # noqa: BLE001
        resp, err = None, f"site unreachable ({type(e).__name__})"
    if resp is None:
        # An oversized page just means no <link> candidates — the common-path
        # guesses below still stand a chance. Anything else is fatal.
        if "too large" not in (err or ""):
            return "", None, err or "site unreachable"
    elif resp.status_code != 200:
        return "", None, f"site returned HTTP {resp.status_code}"
    else:
        ctype = (resp.headers.get("content-type") or "").lower() if getattr(resp, "headers", None) else ""
        body = resp.content or b""
        if "html" in ctype or body[:512].lstrip().lower().startswith((b"<!doctype", b"<html")):
            try:
                html = body[:DISCOVER_HTML_MAX].decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                html = ""
            # Resolve relative hrefs against the *hostname* URL, never the
            # pinned-IP request URL (guarded_get exposes it as logical_url).
            base = str(getattr(resp, "logical_url", "") or site_url)
            cands.extend(html_feed_links(html, base))
    root = httpx.URL(site_url)
    origin = f"{root.scheme}://{root.host}" + (f":{root.port}" if root.port else "")
    for pth in GUESS_PATHS:
        c = origin + pth
        if c not in cands:
            cands.append(c)
    tried = 0
    for c in cands:
        if tried >= max_try:
            break
        tried += 1
        p = await feeds.read_feed(c, limit=1)
        if p.get("ok"):
            return c, p, ""
    return "", None, ("no feed found for this site (it may not publish RSS, "
                      "or hides it too well)")
