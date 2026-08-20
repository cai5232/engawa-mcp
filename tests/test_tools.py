"""Tool-surface behavior: argument validation, receipts, shelf write paths."""
import pytest

from engawa_mcp import config, net, server, shelf


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return tmp_path


# ── web_read ────────────────────────────────────────────────────────────

async def test_web_read_blocks_x_dot_com():
    out = await server.web_read("https://x.com/someone/status/1")
    assert out["content"] == "" and "login wall" in out["note"]
    out = await server.web_read("https://twitter.com/someone")
    assert "login wall" in out["note"]


async def test_web_read_blocks_private():
    out = await server.web_read("http://127.0.0.1:8080/admin")
    assert out["content"] == "" and "private" in out["note"]


async def test_web_read_bad_scheme():
    out = await server.web_read("file:///etc/passwd")
    assert out["content"] == "" and out["note"] != ""


# ── validation receipts ─────────────────────────────────────────────────

async def test_sky_tonight_bad_date():
    out = await server.sky_tonight("2026/01/01")
    assert out["ok"] is False and "YYYY-MM-DD" in out["note"]


async def test_apod_bad_date():
    out = await server.apod("yesterday")
    assert out["ok"] is False and "YYYY-MM-DD" in out["note"]


async def test_arxiv_bad_category():
    out = await server.arxiv_new("cs.AI; DROP TABLE")
    assert out["ok"] is False and "category" in out["note"]


async def test_on_this_day_bad_lang():
    out = await server.on_this_day("EN US")
    assert out["ok"] is False


async def test_daily_art_unreachable(monkeypatch):
    async def fake_get_json(url, params=None, headers=None, timeout=15.0):
        return None, "timeout"
    monkeypatch.setattr(net, "get_json", fake_get_json)
    out = await server.daily_art()
    assert out["ok"] is False and "unreachable" in out["note"]


# ── shelf tools ─────────────────────────────────────────────────────────

async def test_shelf_lists_builtins():
    out = await server.shelf_list()
    assert out["ok"] is True
    assert len(out["entries"]) == len(shelf.BUILTIN)
    assert "rss_read" in out["note"]


async def test_shelf_tag_filter_and_unknown_tag():
    out = await server.shelf_list("ocean")
    assert out["ok"] and all("ocean" in e["tags"] for e in out["entries"])
    out = await server.shelf_list("fish")
    assert out["ok"] is False


async def test_shelf_suggest_marks_on_shelf():
    out = await server.shelf_suggest()
    assert out["ok"] and len(out["entries"]) == len(shelf.SUGGEST)
    assert all(e["on_shelf"] is False for e in out["entries"])


async def test_shelf_add_rejects_shelf_name_as_url():
    out = await server.shelf_add(url="aeon")
    assert out["ok"] is False and "full site address" in out["note"]


async def test_shelf_add_rejects_bad_name_and_tags():
    out = await server.shelf_add(url="https://a.example", name="Bad Name")
    assert out["ok"] is False
    out = await server.shelf_add(url="https://a.example", tags="science,fish")
    assert out["ok"] is False and "fish" in out["note"]


async def test_shelf_add_rejects_builtin_name():
    out = await server.shelf_add(url="https://a.example", name="aeon")
    assert out["ok"] is False and "built-in" in out["note"]


def _fake_discover(feed_url, title="Some Feed"):
    async def fake(url, max_try=shelf.DISCOVER_MAX_TRY):
        return feed_url, {"ok": True, "feed_title": title, "entries": [], "note": ""}, ""
    return fake


async def test_shelf_add_and_remove_roundtrip(monkeypatch):
    monkeypatch.setattr(shelf, "discover_feed", _fake_discover("https://new.example/feed"))
    out = await server.shelf_add(url="https://new.example", tags="wonder", note="n",
                                 picked_by="me")
    assert out["ok"] is True and out["note"] == "added"
    name = out["name"]

    listed = await server.shelf_list("wonder")
    mine = [e for e in listed["entries"] if e["name"] == name]
    assert mine and mine[0]["source"] == "user" and mine[0]["picked_by"] == "me"

    # Same feed again with a different name → refused, points at the holder.
    out2 = await server.shelf_add(url="https://new.example", name="other-name")
    assert out2["ok"] is False and out2["name"] == name

    out3 = await server.shelf_remove(name)
    assert out3["ok"] is True
    out4 = await server.shelf_remove(name)
    assert out4["ok"] is False


async def test_shelf_add_dedups_against_builtin(monkeypatch):
    monkeypatch.setattr(shelf, "discover_feed",
                        _fake_discover(shelf.BUILTIN["aeon"]["url"]))
    out = await server.shelf_add(url="https://aeon.co")
    assert out["ok"] is False and "aeon" in out["note"]


async def test_shelf_add_update_path(monkeypatch):
    monkeypatch.setattr(shelf, "discover_feed", _fake_discover("https://u.example/feed"))
    first = await server.shelf_add(url="https://u.example", name="mysrc")
    assert first["ok"] and first["note"] == "added"
    second = await server.shelf_add(url="https://u.example", name="mysrc", note="new note")
    assert second["ok"] and second["note"] == "updated"


async def test_shelf_remove_builtin_refused():
    out = await server.shelf_remove("aeon")
    assert out["ok"] is False and "built-in" in out["note"]


async def test_shelf_writes_refused_while_file_corrupt(tmp_cache, monkeypatch):
    (tmp_cache / "shelf.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(shelf, "discover_feed", _fake_discover("https://c.example/feed"))
    out = await server.shelf_add(url="https://c.example")
    assert out["ok"] is False and "corrupt" in out["note"]
    out = await server.shelf_remove("anything")
    assert out["ok"] is False and "corrupt" in out["note"]


# ── registry ────────────────────────────────────────────────────────────

async def test_twelve_tools_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"web_read", "rss_read", "shelf", "shelf_add", "shelf_suggest",
                     "shelf_remove", "sky_tonight", "apod", "daily_art",
                     "arxiv_new", "daily_poem", "on_this_day"}


def test_check_cli_ok():
    from engawa_mcp.__main__ import _check
    assert _check() == 0
