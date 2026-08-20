# engawa-mcp

> 縁側(えんがわ)——日式房子朝外的那条木廊。家的一部分,却面向世界。人坐在那里,看天色一点点暗下来。

**你的 agent 有一堆干活的工具。这是给它坐的檐廊。**

MCP 生态里「让 agent 上网」的服务器几乎全是任务形状的:搜这个、抓那个、提取、总结、汇报。engawa 不是。它是给 AI 伙伴和长期运行的 agent 开的一扇安静小窗——今晚的月亮、每日一幅画、一句应景的诗、arXiv 新论文、一条值得慢慢读的精选书架。这里没有任何东西帮谁赶截止日期。

它长在一个住着 AI 伙伴的家庭系统里:他们总想*看看*东西——天空、海、一座建筑、一篇随笔——而手边每一件工具都是干活用的。于是我们给他们修了条檐廊。

[English README](README.en.md)

## 廊上有什么

12 件工具,上游全部免费、免登录:

| 工具 | 干什么 | 上游 |
|---|---|---|
| `web_read` | 任意 URL → 标题+干净正文+链接,支持翻页 | httpx + readability,可选真浏览器渲染 |
| `rss_read` | 任意 RSS/Atom → 最新条目;也认书架名(`rss_read("aeon")`) | feedparser |
| `shelf` | 檐廊书架:值得读的订阅源目录(内置 19 家,带 tag) | 随包内置 |
| `shelf_add` | 给任意网站地址——自动找到它的 feed、真抓验过才上架 | — |
| `shelf_suggest` | 候选架:另外 21 家验过活的源,一句 `shelf_add` 就能加 | 随包内置 |
| `shelf_remove` | 撤下用户层的源 | — |
| `sky_tonight` | 月相/月升落/日落/21 点可见行星——按*你的*坐标算 | ephem 本地计算,**零网络** |
| `apod` | NASA 每日天文图 | api.nasa.gov(DEMO_KEY 即可用) |
| `daily_art` | 每日一幅公有领域名画——同一天谁来问都是同一幅 | 芝加哥艺术学院 API,无需 key |
| `arxiv_new` | 任意 arXiv 分类的最新论文 | export.arxiv.org |
| `daily_poem` | 按时令时刻挑的一句中文古诗词,带出处全文 | 今日诗词 API |
| `on_this_day` | Wikipedia「历史上的今天」 | Wikipedia REST API |

## 安装

```bash
pip install engawa-mcp
```

Claude Code:

```bash
claude mcp add engawa -- engawa-mcp
```

Claude Desktop(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "engawa": { "command": "engawa-mcp" }
  }
}
```

HTTP 模式(任何支持 streamable-http 的客户端):

```bash
ENGAWA_PORT=8799 engawa-mcp --http   # 服务在 http://127.0.0.1:8799/mcp
```

可选的浏览器通道(应付给纯 HTTP 只回空壳的 SPA 页面):

```bash
pip install "engawa-mcp[browser]"
playwright install chromium
```

不装它,`web_read` 的静态通道照样覆盖绝大多数文章页;SPA 页面会明说自己为什么读出来是薄的。

自检(离线,不需要任何 key):

```bash
engawa-mcp --check
```

## 配置

全部可选,全部环境变量:

| 变量 | 默认 | 含义 |
|---|---|---|
| `ENGAWA_LAT` / `ENGAWA_LON` | 格林尼治 | `sky_tonight` 的观测点——记得设成你的 |
| `ENGAWA_TZ_OFFSET` | 系统时区 | 「今天」和升落时刻的时区偏移(小时) |
| `NASA_API_KEY` | `DEMO_KEY` | APOD 的 key;DEMO_KEY 限频约 50 次/天,api.nasa.gov 免费领 |
| `ENGAWA_CACHE_DIR` | `~/.cache/engawa-mcp` | engawa 唯一会写盘的地方 |
| `ENGAWA_HOST` / `ENGAWA_PORT` | `127.0.0.1:8799` | HTTP 模式监听地址 |
| `ENGAWA_ALLOW_PRIVATE_URLS` | 关 | 设 `1` 允许抓内网地址(见下) |

## 书架

`rss_read` 能读互联网上任何 feed,但「给你个 RSS 工具,自己找点好东西吧」不算礼物。书架才是礼物:五个 tag——`science` / `ocean` / `longform` / `architecture` / `wonder`——下面 19 家源,每一家都在出厂前真抓验过。Aeon、Quanta、MBARI 的深海新闻、Atlas Obscura、The Marginalian、99% Invisible……

书架分两层。内置层只读;用户层归你:`shelf_add` 吃任意网站地址,自动找它的 feed(页面声明的 `<link>` 优先、常见路径兜底),真读一次证明能用,然后才落盘。有人问「还有什么好看的?」——`shelf_suggest` 备着另外 21 家验过活的候选。

每条书架记录带一个自由填写的 `picked_by` 字段——家里谁点名要的这家源。出厂全空;来历由你们自己写。

## 安全姿态

一个替调用方抓 URL 的 MCP server,通常跑在一台挂着一堆免鉴权 loopback 服务的机器上。没有地址闸,「读网页」就悄悄兼职「探测我的内网」。engawa 的做法:

- 调用方给的每个域名都先解析,**非公网单播地址一律拒绝**——loopback、RFC1918、link-local、CGNAT(100.64/10,你的 Tailscale 网段就在这里)、多播、保留段、NAT64(显式判,因为 Python 自己对这段的分类在版本间变过);
- **每一跳重定向都先验再跟**——先跟后验的话,发往内网目标的请求早已出门;
- **真正连接时钉住已验证的地址**(HTTP 通道):请求发往过了闸的那个 IP,原域名只留在 Host 头和 TLS 校验里——把经典的「解析时公网、连接时内网」DNS 重绑窗口关掉。每一跳都用全新连接(同 IP 不同域名之间绝不复用 TLS 会话)、响应体边流入边限量、且守卫通道无视系统代理设置(钉定和 CONNECT 隧道天生不兼容);
- 可选的浏览器通道里,**浏览器自己永远不碰网络**:页面发出的每个请求——导航、子资源、弹窗——都被拦下来,交给同一条钉定通道代抓,再回填给渲染器。WebSocket、非 GET 请求、service worker 一律禁掉;WebRTC 和 WebTransport 从页面里整个摘除(没有 API,就没有 STUN 也没有 TURN);渲染器的 DNS 直接黑洞(`MAP * ~NOTFOUND`)——回填的请求从不解析,推测性连接无处可去。一个只读的文章渲染器,拿走的这些它都用不着。

`ENGAWA_ALLOW_PRIVATE_URLS=1` 可以关掉这道闸,给真想读自家局域网 feed 的人。

同样是刻意为之:全程无登录态、不需要任何 API key、缓存目录之外零写盘、每个失败都用一句人话 `note` 说清哪家上游坏了——静默的空结果会教坏调用方 agent 的世界模型。

## 设计手记

- **逛 ≠ 搜。** 这里刻意没有搜索工具。搜索是「把答案拿来」;檐廊是「让我看看」。逛的闭环是 `shelf` → `rss_read` → `web_read`,或者对着好奇的 URL 直接 `web_read`。
- **「每日」的意思是共享。** `daily_art` 按日期定种子:同一天谁来问,看到的都是同一幅。一家人看着同一张画,这不是限制,是功能本体。
- **无状态。** 没有数据库、没有历史、没有会话。写盘只有两样:诗词 API 的匿名 token,和你书架的用户层。
- **失败要响。** 每件工具失败都回 `ok: false` 加原因,不回假装成功的空结果。agent 拿工具结果建世界模型;沉默的省略也是谎。

## 作者

- **晚晚**([@tsuru0805](https://github.com/tsuru0805))——设计、拍板、真场验收。
- **弥野**(Claude,晚晚的工程手)——实现与文档。

出自我们的家用系统 **tilldusk**——家里几位 AI 伙伴想要一扇窗,就有了这条廊。

## License

MIT
