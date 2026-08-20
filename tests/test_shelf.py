"""Shelf layers: validation, persistence, shadowing, name derivation."""
import json
from datetime import date

import pytest

from engawa_mcp import config, shelf


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return tmp_path


def test_builtin_shape():
    assert len(shelf.BUILTIN) == 19
    for name, e in shelf.BUILTIN.items():
        assert shelf.NAME_RE.match(name)
        assert e["url"].startswith("https://")
        assert e["tags"] and all(t in shelf.TAGS for t in e["tags"])
        assert e["picked_by"] == ""      # ships anonymous; provenance is yours to fill


def test_suggest_shape():
    assert len(shelf.SUGGEST) == 21
    overlap = set(shelf.SUGGEST) & set(shelf.BUILTIN)
    assert not overlap


def test_entry_valid_rejects_bad_records():
    good = {"url": "https://a.example/feed", "tags": ["science"],
            "title": "A", "note": "", "picked_by": ""}
    assert shelf.entry_valid("aa", good) is not None
    assert shelf.entry_valid("Bad Name", good) is None
    assert shelf.entry_valid("aa", {**good, "url": "gopher://x"}) is None
    assert shelf.entry_valid("aa", {**good, "tags": None}) is None
    assert shelf.entry_valid("aa", {**good, "tags": ["nope"]}) is None
    assert shelf.entry_valid("aa", {**good, "title": 3}) is None


def test_user_load_corrupt_file(tmp_cache):
    (tmp_cache / "shelf.json").write_text("{not json", encoding="utf-8")
    entries, err = shelf.user_load()
    assert entries == {} and "corrupt" in err


def test_user_load_skips_invalid_records(tmp_cache):
    (tmp_cache / "shelf.json").write_text(json.dumps({
        "good": {"url": "https://a.example/feed", "tags": [], "title": "", "note": "", "picked_by": ""},
        "BAD": {"url": "https://b.example/feed", "tags": []},
    }), encoding="utf-8")
    entries, err = shelf.user_load()
    assert set(entries) == {"good"} and "BAD" in err


def test_save_roundtrip(tmp_cache):
    data = {"mysrc": {"url": "https://a.example/feed", "tags": ["ocean"],
                      "title": "T", "note": "n", "picked_by": "me"}}
    shelf.user_save(data)
    loaded, err = shelf.user_load()
    assert err == "" and loaded == data
    assert not list(tmp_cache.glob("*.tmp"))   # no temp files left behind


def test_merged_shadowing(tmp_cache):
    shelf.user_save({"aeon": {"url": "https://elsewhere.example/feed", "tags": [],
                              "title": "", "note": "", "picked_by": ""}})
    merged, err = shelf.merged()
    assert merged["aeon"]["source"] == "builtin"
    assert "shadowed" in err


def test_resolve():
    url, err = shelf.resolve("aeon")
    assert err == "" and url == shelf.BUILTIN["aeon"]["url"]
    url, err = shelf.resolve("https://x.example/feed")
    assert err == "" and url == "https://x.example/feed"
    _, err = shelf.resolve("no-such-name")
    assert "shelf" in err
    _, err = shelf.resolve("")
    assert err != ""


def test_norm_feed_url():
    assert shelf.norm_feed_url("HTTPS://Nautil.us/feed/") == shelf.norm_feed_url("https://nautil.us/feed")
    assert shelf.norm_feed_url("https://a.example/f#frag") == shelf.norm_feed_url("https://a.example/f")
    assert shelf.norm_feed_url("https://a.example/f?x=1") != shelf.norm_feed_url("https://a.example/f")


def test_auto_name():
    assert shelf.auto_name("https://www.nautil.us/feed/", set()) == "nautil"
    assert shelf.auto_name("https://feeds.bbci.co.uk/news/rss.xml", set()) == "bbci"
    assert shelf.auto_name("https://nautil.us/feed", {"nautil"}) == "nautil-2"
    assert shelf.auto_name("https://例え.jp/feed", set()).startswith("site")


def test_html_feed_links():
    html = ('<head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            '<link rel="stylesheet" href="/style.css">'
            "<link rel='alternate' type='application/atom+xml' href='https://abs.example/atom'>"
            '</head>')
    out = shelf.html_feed_links(html, "https://base.example/page")
    assert out == ["https://base.example/feed.xml", "https://abs.example/atom"]


def test_sky_smoke():
    # Not a shelf concern, but cheap to pin here: compute() is pure and total.
    from engawa_mcp import sky
    out = sky.compute(date(2026, 1, 3))
    assert out["moon_phase"] in sky.PHASE_NAMES
    assert out["moon_illumination"].endswith("%")
    for key in ("moonrise", "moonset", "sunset", "next_full_moon", "planets_2100"):
        assert key in out
