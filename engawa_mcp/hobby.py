"""The hobby corner: news on topics the user picked.

A topic is a keyword ("sourdough", "大熊猫", "編み物"). Its news comes from
Bing News' keyword RSS — free, no key, newest first, and every item carries
the publisher's own address (unwrapped from Bing's click tracker in
feeds.read_feed), so web_read can open the article itself.

Shelf sources tagged "hobby" join the corner too, for people who already know
a site they like.

This is a subscription, not a search box: the topic list lives in
CACHE_DIR/hobbies.json (atomic writes; a corrupt file is reported and writes
are refused until it is fixed, never silently overwritten).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import config, feeds, shelf

log = logging.getLogger("engawa.hobby")

BING_NEWS = "https://www.bing.com/news/search"
TOPICS_MAX = 12
TOPIC_MAX_LEN = 40
EXCLUDE_MAX = 8
EXCLUDE_WORD_MAX = 20
PICKED_BY_MAX = shelf.PICKED_BY_MAX
FETCH_PER_SOURCE = 15     # raw items pulled per source before filtering
PER_SOURCE_MAX = 10       # hobby_corner per_topic upper bound
SUMMARY_MAX = 200
CORNER_BUDGET_S = 40.0    # wall clock for one hobby_corner call
CORNER_CONCURRENCY = 4
DUP_THRESHOLD = 0.4       # title-token containment at/above this = same story (tuned on live headlines, zh/ja/en)

# Precise, technical: what an unreachable Bing usually means for the caller.
UNREACHABLE_HINT = (
    "Bing News could not be read from this machine. From mainland China, "
    "www.bing.com redirects to cn.bing.com, which serves no news RSS; engawa "
    "ignores system proxy settings, so a VPN only helps in TUN/global mode.")

lock = asyncio.Lock()


def _path() -> Path:
    return config.CACHE_DIR / "hobbies.json"


def key(topic: str) -> str:
    """Matching key: NFKC + casefold + collapsed whitespace."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", topic or "")).strip().casefold()


def clean_topic(raw: str) -> tuple[str, str]:
    """User text → stored topic. Returns (topic, err)."""
    t = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw or "")).strip()
    if not t:
        return "", "topic is empty"
    if len(t) > TOPIC_MAX_LEN:
        return "", f"topic too long (max {TOPIC_MAX_LEN} characters)"
    if any(unicodedata.category(c).startswith("C") for c in t):
        return "", "topic contains control characters"
    return t, ""


def parse_exclude(raw: str) -> tuple[list[str], str]:
    """Comma-separated words (ASCII or CJK commas). Returns (words, err)."""
    out: list[str] = []
    for w in re.split(r"[,，、;；]", unicodedata.normalize("NFKC", raw or "")):
        w = re.sub(r"\s+", " ", w).strip()
        if not w:
            continue
        if len(w) > EXCLUDE_WORD_MAX:
            return [], f"exclude word too long: '{w[:EXCLUDE_WORD_MAX]}…' (max {EXCLUDE_WORD_MAX})"
        if any(unicodedata.category(c).startswith("C") for c in w):
            return [], "exclude words contain control characters"
        if key(w) not in {key(x) for x in out}:
            out.append(w)
    if len(out) > EXCLUDE_MAX:
        return [], f"too many exclude words (max {EXCLUDE_MAX})"
    return out, ""


def record_valid(v) -> dict | None:
    if not isinstance(v, dict):
        return None
    topic, terr = clean_topic(v.get("topic") if isinstance(v.get("topic"), str) else "")
    if terr:
        return None
    ex = v.get("exclude", [])
    if not (isinstance(ex, list) and all(isinstance(w, str) for w in ex)):
        return None
    words, eerr = parse_exclude(",".join(ex))
    if eerr:
        return None
    out = {"topic": topic, "exclude": words}
    for f in ("picked_by", "added"):
        val = v.get(f, "")
        if not isinstance(val, str):
            return None
        out[f] = val
    return out


def load() -> tuple[list[dict], str]:
    """Returns (topics, err). Never raises; while err is set, writes are refused
    (saving would drop the records that failed to load)."""
    p = _path()
    if not p.exists():
        return [], ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to parse %s: %s", p, type(e).__name__)
        return [], (f"hobbies.json is corrupt ({type(e).__name__}); treated as "
                    "empty — fix or delete it before adding/removing")
    raw = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], "hobbies.json has no 'topics' list; treated as empty"
    good: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for v in raw:
        rec = record_valid(v)
        if rec is None or key(rec["topic"]) in seen:
            skipped += 1
            continue
        seen.add(key(rec["topic"]))
        good.append(rec)
    err = (f"skipped {skipped} invalid or duplicate hobby records "
           "(fix or remove them before adding/removing)" if skipped else "")
    return good, err


def save(topics: list[dict]) -> None:
    shelf.atomic_write_json(_path(), {"topics": topics})


def find(topics: list[dict], topic: str) -> int:
    k = key(topic)
    return next((i for i, t in enumerate(topics) if key(t["topic"]) == k), -1)


def news_url(topic: str) -> str:
    params = {"q": topic, "format": "rss", "qft": 'sortbydate="1"'}
    if config.NEWS_LANG:
        params["setlang"] = config.NEWS_LANG
    return f"{BING_NEWS}?{urlencode(params)}"


# ── near-duplicate merging ────────────────────────────────────────────

# Scripts written without spaces between words get character bigrams:
# Thai, Lao, Myanmar, Khmer, kana, CJK ideographs (all extension planes),
# hangul syllables.
_DENSE_RANGES = ((0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF),
                 (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
                 (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0x20000, 0x3FFFF))


def _dense(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DENSE_RANGES)


def tokens(text: str) -> set[str]:
    """Words (>=3 chars) for every other script, character bigrams for
    scripts without spaces. Letter bigrams would make every English headline
    look alike, so the two families are tokenized differently. A word is a
    run of Unicode letters, digits and combining marks (category L/N/M), so
    Bengali vowel signs or Hebrew points stay inside their word."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    out: set[str] = set()
    word: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(word) >= 3:
            out.add("".join(word))
        if len(run) == 1:
            out.add(run[0])
        out.update(run[i] + run[i + 1] for i in range(len(run) - 1))
        word.clear()
        run.clear()

    for ch in t:
        cat = unicodedata.category(ch)[0]
        if _dense(ch) and cat in "LMN":
            if word:
                flush()
            run.append(ch)
        elif cat in "LMN":
            if run:
                flush()
            word.append(ch)
        else:
            flush()
    flush()
    return out


def same_story(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= DUP_THRESHOLD


def merge_duplicates(items: list[dict], topic: str = "") -> list[dict]:
    """Keep the first (newest) report of each story; later near-identical
    headlines are folded into its `also_reported` count. The topic's own
    tokens are ignored — every headline shares them."""
    drop = tokens(topic)
    kept: list[tuple[set[str], dict]] = []
    for it in items:
        tk = tokens(it["title"]) - drop
        hit = next((k for k in kept if same_story(tk, k[0])), None)
        if hit:
            hit[1]["also_reported"] += 1
        else:
            kept.append((tk, {**it, "also_reported": 0}))
    return [d for _, d in kept]


def excluded(title: str, words: list[str]) -> bool:
    t = key(title)
    return any(key(w) in t for w in words)


def _link_site(link: str) -> str:
    """The display site for an item link, or "" if the link is unusable:
    missing, not http(s), unparsable, or a Bing redirect that could not be
    unwrapped (it would not be the publisher's page)."""
    try:
        u = urlparse(link or "")
        host = (u.hostname or "").lower()
        u.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return ""
    if u.scheme.lower() not in ("http", "https") or not host:
        return ""
    if host in feeds.BING_HOSTS and u.path.lower() == "/news/apiclick.aspx":
        return ""  # read_feed already unwrapped every wrapper it could
    return host[4:] if host.startswith("www.") else host


def _items(entries: list[dict]) -> tuple[list[dict], int]:
    """Feed entries -> corner items. Returns (items, skipped); skipped counts
    entries dropped for an unusable link and is always reported."""
    out, skipped = [], 0
    for e in entries:
        link = e.get("link", "")
        site = _link_site(link)
        if not site:
            skipped += 1
            continue
        out.append({"title": e.get("title", ""), "url": link, "site": site,
                    "published": e.get("published", ""),
                    "summary": (e.get("summary", "") or "")[:SUMMARY_MAX]})
    return out, skipped


def _skipped_note(n: int) -> str:
    return (f"{n} item(s) skipped: link missing, not http(s), or an unresolved "
            "Bing redirect") if n else ""


# ── fetching ──────────────────────────────────────────────────────────

async def probe_topic(topic: str) -> dict:
    """One real fetch for hobby_add: is Bing reachable, what comes back."""
    got = await feeds.read_feed(news_url(topic), limit=FETCH_PER_SOURCE)
    if not got.get("ok"):
        return {"ok": False, "note": f"{got.get('note', 'fetch failed')}. {UNREACHABLE_HINT}"}
    items, skipped = _items(got["entries"])
    return {"ok": True, "items": merge_duplicates(items, topic), "note": _skipped_note(skipped)}


async def fetch_topic(rec: dict, per: int) -> dict:
    row = {"topic": rec["topic"], "kind": "keyword", "exclude": list(rec["exclude"]),
           "ok": False, "note": "", "items": []}
    got = await feeds.read_feed(news_url(rec["topic"]), limit=FETCH_PER_SOURCE)
    if not got.get("ok"):
        row["note"] = f"{got.get('note', 'fetch failed')}. {UNREACHABLE_HINT}"
        return row
    items, skipped = _items(got["entries"])
    kept = [it for it in items if not excluded(it["title"], rec["exclude"])]
    dropped = len(items) - len(kept)
    kept = merge_duplicates(kept, rec["topic"])[:per]
    row.update(ok=True, items=kept)
    notes = [_skipped_note(skipped)] if skipped else []
    if dropped:
        notes.append(f"{dropped} headline(s) dropped by exclude words")
    if not kept:
        notes.append("no recent news matched this topic")
    row["note"] = "; ".join(notes)
    return row


async def fetch_site(name: str, entry: dict, per: int) -> dict:
    row = {"topic": name, "kind": "site", "title": entry.get("title", name),
           "ok": False, "note": "", "items": []}
    got = await feeds.read_feed(entry["url"], limit=FETCH_PER_SOURCE)
    if not got.get("ok"):
        row["note"] = got.get("note", "fetch failed")
        return row
    items, skipped = _items(got["entries"])
    kept = merge_duplicates(items)[:per]
    notes = [_skipped_note(skipped)] if skipped else []
    if not kept:
        notes.append("the feed has no usable entries")
    row.update(ok=True, items=kept, note="; ".join(notes))
    return row


async def corner(per: int, only: str = "") -> dict:
    topics, terr = load()
    merged, serr = shelf.merged()
    sites = {n: e for n, e in merged.items() if "hobby" in e.get("tags", [])}
    if only:
        k = key(only)
        topics = [t for t in topics if key(t["topic"]) == k]
        sites = {n: e for n, e in sites.items() if n == k}
    jobs: list[tuple[dict, object]] = []
    for t in topics:
        jobs.append(({"topic": t["topic"], "kind": "keyword", "exclude": list(t["exclude"])},
                     lambda t=t: fetch_topic(t, per)))
    for n, e in sites.items():
        jobs.append(({"topic": n, "kind": "site", "title": e.get("title", n)},
                     lambda n=n, e=e: fetch_site(n, e, per)))

    warn = "; ".join(x for x in (terr, serr) if x)
    if not jobs:
        if only:
            msg = f"no hobby named '{only}'"
            return {"ok": False, "hobbies": [], "note": f"⚠ {warn}. {msg}" if warn else msg}
        if warn:  # nothing to show *because* the saved lists are unreadable
            return {"ok": False, "hobbies": [], "note": f"⚠ {warn}"}
        return {"ok": True, "hobbies": [],
                "note": ("no hobbies yet — hobby_add(topic) adds a keyword topic; "
                         "shelf_add(url, tags='hobby') adds a site")}

    sem = asyncio.Semaphore(CORNER_CONCURRENCY)

    async def run(factory):
        async with sem:
            return await factory()

    tasks = [asyncio.ensure_future(run(f)) for _, f in jobs]
    try:
        done, _pending = await asyncio.wait(tasks, timeout=CORNER_BUDGET_S)
    finally:
        # Budget overrun, or this call itself cancelled: stop the stragglers
        # and let them unwind before returning.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    rows = []
    for (stub, _), task in zip(jobs, tasks):
        if task in done and not task.cancelled() and task.exception() is None:
            rows.append(task.result())
        elif task in done and not task.cancelled():
            rows.append({**stub, "ok": False, "items": [],
                         "note": f"fetch failed: {type(task.exception()).__name__}"})
        else:
            rows.append({**stub, "ok": False, "items": [],
                         "note": f"not fetched within {int(CORNER_BUDGET_S)}s"})
    ok = any(r["ok"] for r in rows)
    notes = []
    if warn:
        notes.append(f"⚠ {warn}")
    if not ok:
        notes.append("every hobby source failed; see each row's note")
    else:
        notes.append("each url is the publisher's page — web_read(url) for the full text")
    return {"ok": ok, "hobbies": rows, "note": ". ".join(notes)}


def today() -> str:
    return datetime.now(config.local_tz()).date().isoformat()
