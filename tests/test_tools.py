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
    assert out["ok"] is False and out["content"] == "" and out["note"] != ""


async def test_web_read_static_lane_blocks_redirect_to_private(monkeypatch):
    """The static lane goes through guarded_get: a public page 302ing to a
    private address must fail without the private request ever being sent."""
    import httpx

    from engawa_mcp import fetching

    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://192.168.1.1/panel"})

    async def no_browser(url):
        return "", "dynamic rendering unavailable"

    monkeypatch.setattr(fetching, "_TRANSPORT_FOR_TESTS", httpx.MockTransport(handler))
    # keep the test hermetic even when the optional browser lane is installed
    monkeypatch.setattr(fetching, "_fetch_dynamic", no_browser)
    out = await server.web_read("http://93.184.216.34/page")
    assert out["ok"] is False and "redirect" in out["note"]
    assert all("192.168.1.1" not in h for h in hits)


async def test_web_read_static_success(monkeypatch):
    import httpx

    from engawa_mcp import fetching

    html = ("<html><head><title>A Long Read</title></head><body><article>"
            + "<p>sentence of body text here </p>" * 40
            + '<a href="/next">continue reading</a></article></body></html>')

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    monkeypatch.setattr(fetching, "_TRANSPORT_FOR_TESTS", httpx.MockTransport(handler))
    out = await server.web_read("http://93.184.216.34/article")
    assert out["ok"] is True
    assert out["title"] == "A Long Read"
    assert out["total_length"] > 500 and "sentence of body" in out["content"]
    assert any(l["url"].endswith("/next") for l in out["links"])
    assert out["note"] == ""   # success stays quiet — no in-progress chatter


async def test_web_read_pdf_no_browser_launch(monkeypatch):
    """A PDF (binary content-type) fails loud without wasting a Chromium
    launch — _fetch_dynamic must not be called."""
    import httpx

    from engawa_mcp import fetching

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                             content=b"%PDF-1.7 ...")

    called = {"dyn": False}

    async def spy(url):
        called["dyn"] = True
        return "", "should not run"

    monkeypatch.setattr(fetching, "_TRANSPORT_FOR_TESTS", httpx.MockTransport(handler))
    monkeypatch.setattr(fetching, "_fetch_dynamic", spy)
    out = await server.web_read("http://93.184.216.34/paper.pdf")
    assert out["ok"] is False and "application/pdf" in out["note"]
    assert called["dyn"] is False


async def test_web_read_html_mislabeled_octet_stream(monkeypatch):
    """Real HTML served as application/octet-stream is sniffed and read, not
    rejected on content-type alone."""
    import httpx

    from engawa_mcp import fetching

    html = ("<!doctype html><html><head><title>Mislabeled</title></head><body>"
            "<article>" + "<p>plenty of real body text here </p>" * 30
            + "</article></body></html>")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"},
                             text=html)

    monkeypatch.setattr(fetching, "_TRANSPORT_FOR_TESTS", httpx.MockTransport(handler))
    out = await server.web_read("http://93.184.216.34/page")
    assert out["ok"] is True and out["title"] == "Mislabeled"


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


async def test_apod_success_mapping(monkeypatch):
    async def fake_get_json(url, params=None, headers=None, timeout=15.0):
        assert params["api_key"]
        return {"title": "T", "date": "2026-08-20", "explanation": "x" * 2000,
                "url": "https://img.example/a.jpg", "hdurl": "https://img.example/hd.jpg",
                "media_type": "image"}, ""
    monkeypatch.setattr(net, "get_json", fake_get_json)
    out = await server.apod()
    assert out["ok"] is True and out["title"] == "T"
    assert len(out["explanation"]) == 1200          # trimmed for receipt size
    assert out["hdurl"].startswith("https://")


async def test_daily_art_success_mapping(monkeypatch):
    async def fake_get_json(url, params=None, headers=None, timeout=15.0):
        assert "openaccess-api.clevelandart.org" in url   # not artic.edu — its URLs are bot-walled
        return {"data": [
            {"id": 1, "title": "No Image", "images": None},
            {"id": 7, "title": "Water Lilies",
             "creators": [{"description": "Claude Monet (French, 1840-1926)"}],
             "creation_date": "1906", "technique": "oil on canvas",
             "images": {"web": {"url": "https://openaccess-cdn.clevelandart.org/x/x_web.jpg"}},
             "url": "https://clevelandart.org/art/1960.81"},
        ]}, ""
    monkeypatch.setattr(net, "get_json", fake_get_json)
    out = await server.daily_art("monet")
    assert out["ok"] is True and out["title"] == "Water Lilies"
    assert out["artist"].startswith("Claude Monet")
    assert out["image_url"].startswith("https://openaccess-cdn.clevelandart.org/")
    assert out["page_url"] == "https://clevelandart.org/art/1960.81"
    assert "artic.edu" not in (out["image_url"] + out["page_url"])


async def test_daily_art_params_and_empty(monkeypatch):
    seen = {}

    async def fake_get_json(url, params=None, headers=None, timeout=15.0):
        seen["url"], seen["params"] = url, params
        return {"data": []}, ""

    monkeypatch.setattr(net, "get_json", fake_get_json)
    out = await server.daily_art()
    p = dict(seen["params"])
    assert seen["url"].startswith("https://openaccess-api.clevelandart.org/")
    assert p["cc0"] == "1" and p["has_image"] == "1" and p["limit"] == "10"
    assert "q" not in p and int(p["skip"]) % 10 == 0 and 0 <= int(p["skip"]) <= 890
    assert out["ok"] is False and "no open-access artworks" in out["note"]
    out = await server.daily_art("moon")
    p = dict(seen["params"])
    assert p.get("q") == "moon" and "skip" not in p
    assert out["ok"] is False and "moon" in out["note"]


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


async def test_shelf_add_save_failure_is_a_receipt(monkeypatch):
    """A disk write failure comes back as ok:false, never as an exception."""
    monkeypatch.setattr(shelf, "discover_feed", _fake_discover("https://s.example/feed"))

    def boom(_data):
        raise OSError("read-only file system")

    monkeypatch.setattr(shelf, "user_save", boom)
    out = await server.shelf_add(url="https://s.example", tags="wonder")
    assert out["ok"] is False and "OSError" in out["note"]


async def test_shelf_remove_save_failure_is_a_receipt(monkeypatch):
    monkeypatch.setattr(shelf, "discover_feed", _fake_discover("https://s.example/feed"))
    ok = await server.shelf_add(url="https://s.example")
    name = ok["name"]

    def boom(_data):
        raise OSError("disk full")

    monkeypatch.setattr(shelf, "user_save", boom)
    out = await server.shelf_remove(name)
    assert out["ok"] is False and "OSError" in out["note"]


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
