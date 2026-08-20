"""Feed reading with a stubbed network layer."""
import pytest

from engawa_mcp import config, feeds, net, server, shelf

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test &amp; Feed</title>
<item><title>First &lt;b&gt;entry&lt;/b&gt;</title><link>https://a.example/1</link>
<pubDate>Mon, 17 Aug 2026 00:00:00 GMT</pubDate>
<description>Hello &lt;i&gt;world&lt;/i&gt;</description></item>
<item><title>Second</title><link>https://a.example/2</link></item>
</channel></rss>"""


class FakeResp:
    def __init__(self, status_code=200, content=b"", headers=None, url=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = url


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return tmp_path


def _stub_get(result):
    async def fake(url, timeout=15.0, transport=None):
        return result, "" if result is not None else "boom"
    return fake


async def test_read_feed_ok(monkeypatch):
    monkeypatch.setattr(net, "guarded_get", _stub_get(FakeResp(200, RSS)))
    out = await feeds.read_feed("http://93.184.216.34/feed", limit=10)
    assert out["ok"] is True
    assert out["feed_title"] == "Test & Feed"
    assert len(out["entries"]) == 2
    assert out["entries"][0]["title"] == "First entry"        # HTML stripped
    assert out["entries"][0]["summary"] == "Hello world"


async def test_read_feed_limit(monkeypatch):
    monkeypatch.setattr(net, "guarded_get", _stub_get(FakeResp(200, RSS)))
    out = await feeds.read_feed("http://93.184.216.34/feed", limit=1)
    assert len(out["entries"]) == 1


async def test_read_feed_rejects_html(monkeypatch):
    monkeypatch.setattr(net, "guarded_get",
                        _stub_get(FakeResp(200, b"<!doctype html><html><body>hi</body></html>")))
    out = await feeds.read_feed("http://93.184.216.34/page")
    assert out["ok"] is False and "not a valid RSS/Atom" in out["note"]


async def test_read_feed_http_error(monkeypatch):
    monkeypatch.setattr(net, "guarded_get", _stub_get(FakeResp(404)))
    out = await feeds.read_feed("http://93.184.216.34/feed")
    assert out["ok"] is False and "404" in out["note"]


async def test_read_feed_guard_blocks_private():
    out = await feeds.read_feed("http://127.0.0.1/feed")
    assert out["ok"] is False and "private" in out["note"]


async def test_rss_read_resolves_shelf_name(monkeypatch):
    seen = {}

    async def fake_read(url, limit=10):
        seen["url"] = url
        return {"ok": True, "feed_title": "t", "entries": [], "note": ""}

    monkeypatch.setattr(feeds, "read_feed", fake_read)
    out = await server.rss_read("aeon")
    assert out["ok"] is True
    assert seen["url"] == shelf.BUILTIN["aeon"]["url"]


async def test_rss_read_unknown_name():
    out = await server.rss_read("definitely-not-a-name")
    assert out["ok"] is False and "shelf" in out["note"]


async def test_discovery_prefers_declared_link(monkeypatch):
    """A site page declaring its feed via <link> gets discovered and probed."""
    calls = []

    async def fake_read(url, limit=10):
        calls.append(url)
        if url.endswith("/found.xml"):
            return {"ok": True, "feed_title": "Found", "entries": [], "note": ""}
        return feeds.err("not a feed", feed_title="", entries=[])

    html = b'<!doctype html><head><link rel="alternate" type="application/rss+xml" href="/found.xml"></head>'

    async def fake_get(url, timeout=15.0, transport=None):
        return FakeResp(200, html, {"content-type": "text/html"}, url=url), ""

    monkeypatch.setattr(feeds, "read_feed", fake_read)
    monkeypatch.setattr(net, "guarded_get", fake_get)
    feed_url, probe, err = await shelf.discover_feed("http://93.184.216.34/")
    assert err == "" and probe["ok"]
    assert feed_url == "http://93.184.216.34/found.xml"


async def test_discovery_gives_up_politely(monkeypatch):
    async def fake_read(url, limit=10):
        return feeds.err("not a feed", feed_title="", entries=[])

    async def fake_get(url, timeout=15.0, transport=None):
        return FakeResp(200, b"<!doctype html><head></head>", {"content-type": "text/html"}, url=url), ""

    monkeypatch.setattr(feeds, "read_feed", fake_read)
    monkeypatch.setattr(net, "guarded_get", fake_get)
    feed_url, probe, err = await shelf.discover_feed("http://93.184.216.34/")
    assert feed_url == "" and probe is None and "no feed found" in err


async def test_discovery_candidate_budget(monkeypatch):
    """No matter how many guesses exist, at most DISCOVER_MAX_TRY+1 probes run
    (the initial is-it-already-a-feed probe, then max_try candidates)."""
    calls = []

    async def fake_read(url, limit=10):
        calls.append(url)
        return feeds.err("not a feed", feed_title="", entries=[])

    async def fake_get(url, timeout=15.0, transport=None):
        return FakeResp(200, b"<!doctype html><head></head>", {"content-type": "text/html"}, url=url), ""

    monkeypatch.setattr(feeds, "read_feed", fake_read)
    monkeypatch.setattr(net, "guarded_get", fake_get)
    await shelf.discover_feed("http://93.184.216.34/")
    assert len(calls) <= shelf.DISCOVER_MAX_TRY + 1
