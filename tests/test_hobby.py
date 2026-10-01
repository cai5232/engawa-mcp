"""Hobby corner: topic storage, Bing link unwrapping, story merging, receipts.
The network is stubbed at feeds.read_feed (or net.guarded_get for the
unwrapping path); nothing here touches the internet."""
import asyncio
import json

import pytest

from engawa_mcp import config, feeds, hobby, net, server, shelf


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "NEWS_LANG", "")
    return tmp_path


def entry(title, link, published="Wed, 01 Oct 2026 08:00:00 GMT", summary=""):
    return {"title": title, "link": link, "published": published, "summary": summary}


def fake_feeds(by_url_part: dict, calls: list | None = None):
    """read_feed stub: the first key contained in the URL decides the answer
    (a list of entries, or a string = failure note)."""
    async def fake(url, limit=10):
        if calls is not None:
            calls.append(url)
        for part, ans in by_url_part.items():
            if part in url:
                if isinstance(ans, str):
                    return {"ok": False, "note": ans, "feed_title": "", "entries": []}
                return {"ok": True, "note": "", "feed_title": "t", "entries": ans[:limit]}
        return {"ok": False, "note": "unexpected url in test", "feed_title": "", "entries": []}
    return fake


PANDA = [
    entry("开启“吃播”！熊猫“福双”抵美后首段视频发布", "https://news.bjd.com.cn/1"),
    entry("视频丨大熊猫“福双”抵美后的“吃播”首秀：状态良好 胃口不错", "https://news.qq.com/2"),
    entry("淡水河谷拟发行首支熊猫债券 募资规模最高达35亿元人民币", "https://www.eeo.com.cn/3"),
    entry("“国庆快乐”端上桌 重庆动物园大熊猫吃上“国庆大餐”", "https://www.chinanews.com.cn/4"),
    entry("重庆动物园大熊猫吃上“国庆大餐”", "https://www.sohu.com/5"),
    entry("秦岭山野间“她力量”接续守护大熊猫", "https://news.sina.com.cn/6"),
]


# ── Bing link unwrapping ──────────────────────────────────────────────

def test_unwrap_bing_apiclick():
    w = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=x"
         "&url=https%3a%2f%2fwww.setn.com%2fnews%2f169095&c=1")
    assert feeds.unwrap_link(w) == "https://www.setn.com/news/169095"


@pytest.mark.parametrize("link", [
    "http://www.bing.com/news/apiclick.aspx?url=javascript%3aalert(1)",
    "http://www.bing.com/news/apiclick.aspx?url=file%3a%2f%2f%2fetc%2fpasswd",
    "http://www.bing.com/news/apiclick.aspx?ref=x",
    "https://evil.example/news/apiclick.aspx?url=https%3a%2f%2fa.example",
    "https://a.example/post/1",
    "",
])
def test_unwrap_leaves_everything_else_alone(link):
    assert feeds.unwrap_link(link) == link


async def test_read_feed_returns_unwrapped_links(monkeypatch):
    rss = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>b</title>'
           b'<item><title>x</title><link>http://www.bing.com/news/apiclick.aspx?'
           b'ref=FexRss&amp;url=https%3a%2f%2fpub.example%2fa%3fid%3d1</link></item>'
           b'</channel></rss>')

    class R:
        status_code, content, headers = 200, rss, {}

    async def fake_get(url, **kw):
        return R(), ""
    monkeypatch.setattr(net, "guarded_get", fake_get)
    out = await feeds.read_feed("http://93.184.216.34/feed")
    assert out["entries"][0]["link"] == "https://pub.example/a?id=1"


async def test_unwrapped_url_still_goes_through_ssrf_guard():
    out = await server.web_read(feeds.unwrap_link(
        "http://www.bing.com/news/apiclick.aspx?url=http%3a%2f%2f127.0.0.1%2fadmin"))
    assert out["ok"] is False and "private" in out["note"]


# ── topic text ────────────────────────────────────────────────────────

def test_clean_topic():
    assert hobby.clean_topic("  大熊猫 ") == ("大熊猫", "")
    assert hobby.clean_topic("ｓｏｕｒｄｏｕｇｈ")[0] == "sourdough"   # NFKC
    assert hobby.clean_topic("")[1]
    assert hobby.clean_topic("x" * 41)[1]
    assert hobby.clean_topic("a\x00b")[1]


def test_parse_exclude():
    assert hobby.parse_exclude("债券，路书、基金, 债券") == (["债券", "路书", "基金"], "")
    assert hobby.parse_exclude("") == ([], "")
    assert hobby.parse_exclude(",".join(f"w{i}" for i in range(9)))[1]
    assert hobby.parse_exclude("x" * 21)[1]


def test_news_url_language(monkeypatch):
    assert "setlang" not in hobby.news_url("熊猫")
    assert "sortbydate" in hobby.news_url("熊猫")
    monkeypatch.setattr(config, "NEWS_LANG", "zh-hans")
    assert "setlang=zh-hans" in hobby.news_url("熊猫")


# ── story merging ─────────────────────────────────────────────────────

def test_merge_same_story_cjk():
    items = hobby._items(PANDA)
    out = hobby.merge_duplicates(items, "熊猫")
    titles = [i["title"] for i in out]
    assert len(out) == 4
    assert titles[0].startswith("开启") and out[0]["also_reported"] == 1      # 福双 ×2
    chongqing = next(i for i in out if "重庆" in i["title"])
    assert chongqing["also_reported"] == 1 and chongqing["site"] == "chinanews.com.cn"


def test_merge_keeps_distinct_english_stories():
    items = hobby._items([
        entry("A Fifth-Generation Baker is Bringing Sourdough Bagels to Park Slope", "https://a.example/1"),
        entry("Is sourdough bread healthier than whole wheat? Dietitians weigh in", "https://b.example/2"),
        entry("This quick water test checks sourdough starter", "https://c.example/3"),
        entry("Sourdough & Co. Bakes Its Way Into Aurora's Southlands", "https://d.example/4"),
        entry("Sourdough & Co. Expanding to Aurora's Southlands", "https://e.example/5"),
    ])
    out = hobby.merge_duplicates(items, "sourdough")
    assert len(out) == 4 and out[3]["also_reported"] == 1


def test_excluded_matches_headline_case_insensitively():
    assert hobby.excluded("Panda BONDS rally", ["bonds"])
    assert not hobby.excluded("大熊猫吃播", ["债券"])


# ── hobby_add / hobby_remove ──────────────────────────────────────────

async def test_add_saves_after_real_fetch_and_samples_respect_exclude(tmp_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}, calls))
    out = await server.hobby_add("熊猫", exclude="债券", picked_by="friend")
    assert out["ok"] is True and out["note"] == "added"
    assert out["exclude"] == ["债券"] and out["topics"] == ["熊猫"]
    assert all("债券" not in s["title"] for s in out["sample"])
    assert calls and "bing.com/news/search" in calls[0]
    data = json.loads((tmp_cache / "hobbies.json").read_text(encoding="utf-8"))
    rec = data["topics"][0]
    assert rec["topic"] == "熊猫" and rec["exclude"] == ["债券"] and rec["picked_by"] == "friend"
    assert rec["added"]


async def test_add_refuses_when_bing_unreachable(tmp_cache, monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": "not a valid RSS/Atom feed"}))
    out = await server.hobby_add("熊猫")
    assert out["ok"] is False and out["note"].startswith("not added")
    assert "cn.bing.com" in out["note"] and "TUN" in out["note"]
    assert not (tmp_cache / "hobbies.json").exists()


async def test_add_with_no_news_is_kept_and_says_so(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": []}))
    out = await server.hobby_add("极冷门的爱好")
    assert out["ok"] is True and out["sample"] == [] and "no recent news" in out["note"]


async def test_add_same_topic_updates_in_place(tmp_cache, monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("Sourdough")
    first = json.loads((tmp_cache / "hobbies.json").read_text())["topics"][0]["added"]
    out = await server.hobby_add("sourdough", exclude="bagel")
    assert out["note"] == "updated" and out["topics"] == ["sourdough"]
    rec = json.loads((tmp_cache / "hobbies.json").read_text())["topics"][0]
    assert rec["exclude"] == ["bagel"] and rec["added"] == first


async def test_add_caps_topic_count(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    for i in range(hobby.TOPICS_MAX):
        assert (await server.hobby_add(f"topic{i}"))["ok"]
    out = await server.hobby_add("one-more")
    assert out["ok"] is False and "at most" in out["note"]


async def test_add_rejects_bad_input_without_fetching(monkeypatch):
    calls = []
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({}, calls))
    assert (await server.hobby_add("   "))["ok"] is False
    assert (await server.hobby_add("熊猫", exclude="x" * 30))["ok"] is False
    assert calls == []


async def test_writes_refused_while_file_corrupt(tmp_cache, monkeypatch):
    (tmp_cache / "hobbies.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    out = await server.hobby_add("熊猫")
    assert out["ok"] is False and "corrupt" in out["note"]
    out = await server.hobby_remove("熊猫")
    assert out["ok"] is False and "corrupt" in out["note"]
    assert (tmp_cache / "hobbies.json").read_text(encoding="utf-8") == "{broken"


async def test_invalid_records_block_writes_but_are_reported(tmp_cache):
    (tmp_cache / "hobbies.json").write_text(json.dumps(
        {"topics": [{"topic": "ok", "exclude": []}, {"topic": "", "exclude": []}]}))
    topics, err = hobby.load()
    assert [t["topic"] for t in topics] == ["ok"] and "skipped 1" in err


async def test_remove(tmp_cache, monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫")
    await server.hobby_add("咖啡")
    out = await server.hobby_remove("熊猫")
    assert out["ok"] is True and out["topics"] == ["咖啡"]
    out = await server.hobby_remove("熊猫")
    assert out["ok"] is False and out["topics"] == ["咖啡"]


# ── hobby_corner ──────────────────────────────────────────────────────

async def test_corner_empty_says_how_to_start():
    out = await server.hobby_corner()
    assert out["ok"] is True and out["hobbies"] == [] and "hobby_add" in out["note"]


async def test_corner_rows_exclude_merge_and_per_topic(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫", exclude="债券")
    out = await server.hobby_corner(per_topic=10)
    row = out["hobbies"][0]
    assert out["ok"] and row["kind"] == "keyword" and row["ok"]
    assert "1 headline(s) dropped" in row["note"]
    assert len(row["items"]) == 3
    assert all(i["url"].startswith("https://") and i["site"] for i in row["items"])
    out = await server.hobby_corner(per_topic=1)
    assert len(out["hobbies"][0]["items"]) == 1


async def test_corner_one_failure_does_not_sink_the_rest(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫")
    await server.hobby_add("咖啡")
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({
        "%E5%92%96%E5%95%A1": "feed returned HTTP 503", "bing.com": PANDA}))
    out = await server.hobby_corner()
    rows = {r["topic"]: r for r in out["hobbies"]}
    assert out["ok"] is True
    assert rows["熊猫"]["ok"] and rows["熊猫"]["items"]
    assert rows["咖啡"]["ok"] is False and "HTTP 503" in rows["咖啡"]["note"]


async def test_corner_all_failed_is_not_ok(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫")
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": "feed timed out"}))
    out = await server.hobby_corner()
    assert out["ok"] is False and "every hobby source failed" in out["note"]
    assert "TUN" in out["hobbies"][0]["note"]


async def test_corner_includes_hobby_tagged_shelf_sites(monkeypatch):
    shelf.user_save({"soapblog": {"url": "https://soap.example/feed", "tags": ["hobby"],
                                  "title": "Soap Blog", "note": "", "picked_by": ""},
                     "other": {"url": "https://other.example/feed", "tags": ["science"],
                               "title": "Other", "note": "", "picked_by": ""}})
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({
        "soap.example": [entry("New cold-process recipe", "https://soap.example/p/1")]}))
    out = await server.hobby_corner()
    assert [r["topic"] for r in out["hobbies"]] == ["soapblog"]
    row = out["hobbies"][0]
    assert row["kind"] == "site" and row["items"][0]["site"] == "soap.example"


async def test_corner_topic_filter(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫")
    await server.hobby_add("咖啡")
    out = await server.hobby_corner(topic="咖啡")
    assert [r["topic"] for r in out["hobbies"]] == ["咖啡"]
    out = await server.hobby_corner(topic="nope")
    assert out["ok"] is False and "no hobby named" in out["note"]


async def test_corner_budget_marks_slow_rows(monkeypatch):
    monkeypatch.setattr(feeds, "read_feed", fake_feeds({"bing.com": PANDA}))
    await server.hobby_add("熊猫")

    async def slow(url, limit=10):
        await asyncio.sleep(5)
    monkeypatch.setattr(feeds, "read_feed", slow)
    monkeypatch.setattr(hobby, "CORNER_BUDGET_S", 0.05)
    out = await server.hobby_corner()
    assert out["ok"] is False and "not fetched within" in out["hobbies"][0]["note"]


async def test_shelf_add_accepts_hobby_tag(monkeypatch):
    async def disc(url):
        return "https://soap.example/feed", {"ok": True, "feed_title": "Soap"}, ""
    monkeypatch.setattr(shelf, "discover_feed", disc)
    out = await server.shelf_add(url="https://soap.example", tags="hobby")
    assert out["ok"] is True
    listed = await server.shelf_list(tag="hobby")
    assert [e["name"] for e in listed["entries"]] == [out["name"]]
