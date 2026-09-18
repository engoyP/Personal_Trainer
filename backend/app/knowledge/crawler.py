"""抓取层：crawl4ai 封装 + 无 Playwright 时的降级后端。

crawl4ai 的价值在于用真实浏览器渲染 + fit_markdown 降噪。
但它的浏览器内核是单独下载的（几百 MB），装不上或下载失败时，
整条链路不应该因此瘫痪 —— 所以留了一个 httpx 降级后端。

降级后端抓不了 JS 渲染的页面，但政府/医院/学术站绝大多数是服务端渲染的，
实际影响有限；metadata 里会标出本轮用了哪个后端。
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from dataclasses import dataclass, field

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 只带 User-Agent 是骗不过大部分反爬的。补齐一套浏览器常规头，
# 能把一部分站的 403 挡回去（剩下的只能靠 crawl4ai 的真浏览器）。
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# 礼貌间隔：同一域名两次请求之间的最小间隔（秒）
POLITE_DELAY = 2.0


@dataclass
class Fetched:
    url: str
    ok: bool = False
    title: str = ""
    markdown: str = ""          # 已剥离导航/页脚的正文
    fit_markdown: str = ""      # crawl4ai 剪枝版，仅留作诊断对比，不参与抽取
    html_title: str = ""
    published_at: str | None = None
    links: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    error: str = ""

    @property
    def text(self) -> str:
        """正文。

        用的是 strip_leading_nav() 处理过的完整 markdown，不是 crawl4ai 的
        fit_markdown —— 后者的剪枝结果不稳定（实测会把正文剪掉、留下侧栏广告）。
        """
        return (self.markdown or "").strip()


# ------------------------------------------------------------------ 工具
def normalize_url(url: str, base: str = "") -> str:
    """去掉 fragment / 追踪参数，补全相对链接，统一小写主机名。"""
    if base:
        url = urllib.parse.urljoin(base, url)
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith(("utm_", "spm", "from", "share"))]
    return urllib.parse.urlunsplit((
        p.scheme.lower() or "https",
        p.netloc.lower(),
        p.path.rstrip("/") or "/",
        urllib.parse.urlencode(q),
        "",
    ))


def same_site(url: str, domain: str) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    d = domain.lower()
    return host == d or host.endswith("." + d)


def _filter_kwargs(cls, wanted: dict) -> dict:
    """只保留目标类构造函数真正接受的参数。

    crawl4ai 从 0.6 到 0.9 改过好几轮配置字段名（wait_until、delay_before_return_html、
    word_count_threshold 的位置都动过）。写死字段名会在升级时直接抛 TypeError，
    所以这里按签名过滤，不支持的参数静默丢弃。
    """
    import inspect

    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return dict(wanted)

    # 有 **kwargs 就照单全收
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(wanted)

    return {k: v for k, v in wanted.items() if k in params}


_md_gen = None


def _markdown_generator():
    """带内容剪枝的 markdown 生成器，全局只建一次。

    没有它，crawl4ai 只给 raw_markdown，正文前后裹着几百行导航链接；
    有了它才有 fit_markdown（去掉导航/页脚/侧栏后的正文）。
    """
    global _md_gen
    if _md_gen is not None:
        return _md_gen

    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    try:
        flt = PruningContentFilter(threshold=0.45, threshold_type="dynamic",
                                   min_word_threshold=20)
    except TypeError:
        flt = PruningContentFilter()

    _md_gen = DefaultMarkdownGenerator(content_filter=flt)
    return _md_gen


# 明显不是文章正文的路径。
# 这份黑名单是拿真实站点踩出来的：现代站点导航里堆着大量
# /certification/get-certified/xxx、/about/leadership/xxx 这类
# 「看起来像 slug、实际是栏目页」的链接，不排掉会浪费大量抓取。
SKIP_SEGMENTS = re.compile(
    r"(^|/)("
    r"tag|tags|category|categories|author|authors|about|contact|login|log-in|signin|"
    r"register|sign-up|search|rss|feed|sitemap|privacy|terms|cookie|legal|"
    r"membership|join|donate|donation|store|shop|cart|checkout|account|"
    r"careers|jobs|press|media-kit|advertise|newsletter|subscribe|"
    r"events?|meetings?|conference|webinar|foundation|sponsor|partners?|"
    r"certification|certifications|education-resources|resource-library|"
    r"advocacy|archive|faqs?|help|support|leadership|governance|"
    r"视频|图片|下载|招聘|广告|关于|联系|登录|注册|搜索"
    r")(/|$)", re.I)

SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|ico|pdf|zip|rar|mp4|mp3|wav|"
                      r"css|js|json|xml|docx?|xlsx?|pptx?)$", re.I)


def _last_segment(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


# 栏目/索引页的文件名。国内政府 CMS 全站都是 .html，
# 「只要有 .html 就是文章」会把 /n315/index.html 这类栏目页全收进来，
# 把每源的配额吃光，真文章（/c…/content.html）反而排不到。
SECTION_STEM = re.compile(r"^(index|default|list|main|home|more|site|all)$", re.I)

# 联盟营销 / 带货路径。Sleep Foundation 之类站点用内容页给床垫导购引流，
# 这类页面的导航会带出一大批 /best-mattress/… 测评，和训练知识无关。
AFFILIATE_PATH = re.compile(
    r"/(best-|mattress|pillow|bedding|deals?|coupons?|discount|shop|buy|product)s?/", re.I)


def _stem(seg: str) -> str:
    return seg.rsplit(".", 1)[0] if "." in seg else seg


def is_article_like(url: str) -> bool:
    """判断一个链接像不像文章详情页。

    刻意放宽：发现阶段重召回，精度交给入库闸门（主题相关度 + 最小正文长度）。
    因为发现阶段的误判只是多抓一个页面，而漏判会直接丢掉一篇文章。

    唯一的例外是**栏目索引页**：它们数量多、100% 会被闸门拒掉，
    但会排在候选列表最前面，把 max_per_source 配额吃干净。
    所以这里提前挡掉「浅层的 index/list/default 页」——
    注意深度条件：CDC 的真文章恰好也叫 …/benefits/index.html（深度 3），
    一刀切会把 CDC 全站误杀。
    """
    p = urllib.parse.urlsplit(url)
    path = p.path or "/"

    if SKIP_EXT.search(path):
        return False
    if SKIP_SEGMENTS.search(path):
        return False
    if AFFILIATE_PATH.search(path):
        return False
    # 根路径和只有一层目录的，基本都是栏目页
    depth = len([s for s in path.split("/") if s])
    if depth == 0:
        return False

    last = _last_segment(path)

    # 0) 浅层的栏目/索引页。必须放在「长数字 id」之前 ——
    #    否则 /n315/n20001395/index.html 会因为 /n20001395 命中数字 id 规则被放行。
    if SECTION_STEM.match(_stem(last)) and depth <= 2:
        return False

    # 1) 路径里有日期 —— 新闻/博客最常见
    if re.search(r"/\d{4}[/-]\d{1,2}", path):
        return True
    # 2) 长数字 id
    if re.search(r"/\d{4,}", path):
        return True
    # 3) 静态页面后缀
    if re.search(r"\.(html?|shtml|php|aspx?|jsp)$", last, re.I):
        return True
    # 4) 明确的文章路径关键词
    low = path.lower()
    if any(k in low for k in ("/article", "/news", "/blog/", "/post", "/detail", "/content")):
        return True
    # 5) 末段是长多词 slug —— 英文站点靠这个。
    #    要求 ≥20 字符且 ≥3 个连字符，才能把
    #    /active-voice-can-being-more-active-... 收进来，
    #    同时把 /exercise-physiologist 这类栏目页挡在外面。
    if len(last) >= 20 and last.count("-") >= 3:
        return True

    return False


# 明确指向「详情页」的路径特征，权重从高到低。
# 用来在发现阶段给候选排序 —— 不是过滤，是排队：
# 列表页里同时混着导航链接和真文章，不排序的话前 N 条全是导航。
_ARTICLE_STRONG: list[tuple[re.Pattern, int]] = [
    (re.compile(r"/c\d{4,}/content\.html?$", re.I), 6),   # 国产政府 CMS 正文
    (re.compile(r"/art/", re.I), 6),                       # 中华医学会
    (re.compile(r"/t\d+_\d+\.html?$", re.I), 5),           # 中国疾控
    (re.compile(r"/\d{4}[/-]\d{1,2}"), 4),                 # 日期路径
    (re.compile(r"/(?:content|detail|article|news|post)s?/", re.I), 3),
]


def article_rank(url: str, base_dir: str = "") -> int:
    """给候选链接打「像不像文章详情页」的分。越高越像。

    发现阶段的候选同时包含栏目导航和真文章。crawl4ai 返回的链接按 DOM 顺序，
    首页/栏目页开头必然是一排主导航 —— 直接取前 N 条等于只抓到导航。
    所以这里排序后取 top-N。

    base_dir 是列表页自身所在目录（如 /n315/、/physical-activity-basics/）。
    同栏目下的链接加最高分：一个栏目的文章基本都挂在它自己的路径下，
    而全局导航（页眉页脚的其它栏目）不在，这是最可靠的区分信号。
    """
    p = urllib.parse.urlsplit(url).path or "/"
    score = 0
    for rx, w in _ARTICLE_STRONG:
        if rx.search(p):
            score += w

    segs = [s for s in p.split("/") if s]
    last = segs[-1] if segs else ""

    score += min(3, max(0, len(segs) - 1))          # 越深越像详情页
    if len(last) >= 20 and last.count("-") >= 3:
        score += 2                                   # 长 slug
    if re.search(r"\d{4,}", last):
        score += 2                                   # 末段是 id
    if SECTION_STEM.match(_stem(last)):
        score -= 4                                   # 栏目页压到底

    bd = base_dir.rstrip("/") + "/" if base_dir and base_dir != "/" else ""
    if bd and p.startswith(bd) and p != bd:
        score += 6                                   # 与列表页同栏目 —— 最强信号
    return score


# ------------------------------------------------------------------ crawl4ai 后端
class Crawl4AIBackend:
    """用 crawl4ai 的真实浏览器抓取。"""

    name = "crawl4ai"

    def __init__(self, headless: bool = True, verbose: bool = False):
        self.headless = headless
        self.verbose = verbose
        self._crawler = None

    @staticmethod
    def available() -> bool:
        try:
            import crawl4ai  # noqa: F401
            return True
        except Exception:
            return False

    async def __aenter__(self):
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        # crawl4ai 各版本 BrowserConfig 的字段名改来改去，
        # 这里用签名过滤，能传的就传，传不了的不报错。
        kwargs = _filter_kwargs(BrowserConfig, {
            "headless": self.headless,
            "verbose": self.verbose,
            "user_agent": UA,
            "enable_stealth": True,      # 0.9.x 用 playwright-stealth 过基础反爬
        })
        self._crawler = AsyncWebCrawler(config=BrowserConfig(**kwargs))
        await self._crawler.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._crawler is not None:
            await self._crawler.__aexit__(*exc)
        self._crawler = None

    async def fetch(self, url: str) -> Fetched:
        from crawl4ai import CacheMode, CrawlerRunConfig

        cfg = CrawlerRunConfig(**_filter_kwargs(CrawlerRunConfig, {
            "cache_mode": CacheMode.BYPASS,   # 每周任务必须抓新的，不能吃缓存
            "word_count_threshold": 80,       # 太短的块直接丢
            "exclude_external_links": True,
            "page_timeout": 45000,
            "wait_until": "domcontentloaded",
            "delay_before_return_html": 0.8,  # 给 JS 渲染留一点时间
            # 关键：不配 content_filter 的话 crawl4ai 不会生成 fit_markdown，
            # 我们只能拿到带整块导航的 raw_markdown —— 结果是模型看到
            # 满屏菜单，直接判定「这不是文章」。降噪必须显式开。
            "markdown_generator": _markdown_generator(),
        }))
        try:
            res = await self._crawler.arun(url=url, config=cfg)
        except Exception as e:  # noqa: BLE001
            return Fetched(url=url, error=f"{type(e).__name__}: {e}")

        if not getattr(res, "success", True):
            return Fetched(url=url, error=str(getattr(res, "error_message", "抓取失败")))

        raw, fit = _split_markdown(res)
        content = strip_leading_nav(raw)

        links: list[str] = []
        raw_links = getattr(res, "links", None) or {}
        if isinstance(raw_links, dict):
            for item in raw_links.get("internal", []) or []:
                href = item.get("href") if isinstance(item, dict) else str(item)
                if href:
                    links.append(href)

        meta = getattr(res, "metadata", None) or {}
        meta_title = (meta.get("title") or "").strip()
        # 有些站点（尤其是政府 CMS）的 <title> 只是站点名，标题在正文 H1 里，
        # 比如中国疾控每篇文章的 title 都是「中国疾病预防控制中心」。
        # 标题参与检索打分（权重 3×）和新闻噪声判断，抓错等于整篇废掉。
        page_title = _better_title(meta_title, _first_h1(raw))
        return Fetched(
            url=url, ok=True,
            title=page_title,
            html_title=meta_title,
            markdown=content, fit_markdown=fit,
            published_at=_pick_date(meta),
            links=links,
            meta={k: meta.get(k) for k in ("title", "description", "author", "published_time") if meta.get(k)},
        )


def _split_markdown(res) -> tuple[str, str]:
    """取出 (原始 markdown, 剪枝后 markdown)。

    crawl4ai 0.9.x 的 result.markdown 是 StringCompatibleMarkdown ——
    一个 str 子类，同时挂着 raw_markdown / fit_markdown 属性。
    关键坑：它 passes isinstance(x, str)，而且这些属性既不在 __dict__ 里
    也不出现在 dir() 里（走的是 __getattr__）。
    第一版就是被 isinstance 判断骗过，永远只拿到整块导航的原文，
    导致模型判定「这不是文章」、相关度算出 0。只能直接 getattr 取。
    """
    md = getattr(res, "markdown", "") or ""
    if isinstance(md, str) and not hasattr(md, "fit_markdown"):
        # 真的是纯字符串（旧版本或降级路径）
        return str(md), ""
    raw = getattr(md, "raw_markdown", "") or (str(md) if isinstance(md, str) else "")
    fit = getattr(md, "fit_markdown", "") or ""
    return raw, fit


# 行内 markdown 链接。crawler 里单独定义一份，避免反向依赖 scoring。
MD_LINK_LOCAL = re.compile(r"\[[^\]]{0,120}\]\([^)\s]{0,300}\)")
# 认定「这是一个正文段落」的最小长度
PROSE_MIN_CHARS = 120
# 正文块检测：锚点之后这么多行里，至少要再有这么多「正文行」。
BODY_WINDOW = 30
BODY_MIN_RUN = 2

# 页脚/样板块的开头标记。命中即从该行起截断。
# 这些块是「纯文本清单」（组织名、栏目名、版权信息），链接密度低、
# 躲得过链接过滤，但塞满主题词，是相关度误判的主要来源。
FOOTER_MARKER = re.compile(
    r"("
    r"友情链接|相关链接|相关阅读|推荐阅读|热门推荐|延伸阅读|更多阅读|"
    r"站点地图|网站地图|联系我们|关于我们|版权声明|版权所有|网站声明|"
    r"法律声明|隐私政策|免责声明|特别声明|主办单位|承办单位|技术支持|"
    r"ICP备|地址[:：]|邮编[:：]|电话[:：]|传真[:：]|"
    r"related (links?|articles?|posts?|reading|content)|quick links|"
    r"follow us|subscribe|newsletter|site ?map|contact us|about us|"
    r"privacy policy|terms of use|disclaimer|copyright|all rights reserved|"
    r"you might also like|recommended (for you|reading)|share this"
    r")", re.I)


def _link_share(line: str) -> float:
    if not line:
        return 1.0
    return sum(len(m.group(0)) for m in MD_LINK_LOCAL.finditer(line)) / len(line)


def _is_prose(line: str) -> bool:
    """这一行像不像正文段落：够长、不是标题/表格/引用、基本不含链接。"""
    s = line.strip()
    if len(s) < PROSE_MIN_CHARS:
        return False
    if s.startswith(("#", ">", "|", "```", "~~~", "---", "===")):
        return False
    return _link_share(s) <= 0.35


def strip_leading_nav(md: str, back_lines: int = 15, tail_lines: int = 12) -> str:
    """砍掉正文前的导航块和正文后的页脚。

    为什么不直接用 crawl4ai 的 fit_markdown：它的 PruningContentFilter 是按
    「内容密度」剪的，实测会留下侧栏推广块、反而把正文剪掉 —— 一篇 ACSM 的
    运动科学文章被剪成了 PCORI 资助公告。这种不可预测的行为在每周批处理里
    很难排查，所以这里改成确定性规则：找到正文块的起点，把它前面的导航丢弃。

    为什么要「成团」判定而不是找到第一句长文本就切：
    站点页顶常有一句孤立的长文本 —— .gov 安全横幅（"A lock () or https:// means
    you've safely connected to the .gov website..."）、Cookie 声明、免责声明。
    它们本身够长且不含链接，第一版会把它们当成正文起点，于是**整块导航被原样留下**。
    实测 CDC 的正文前 3000 字全是菜单 / Related Topics / 重复 logo，
    模型读完后直接判「这不是文章」。所以这里要求锚点之后一段窗口内
    还有别的正文行（导航块里不会有连续的长正文句），把这类孤立长句跳过。

    规则：
      1. 从上往下找第一个「正文行成团」的位置 → 正文起点
      2. 从起点往上回退最多 back_lines 行，把最近的标题一起带上
      3. 从下往上找最后一个正文行，之后再多留 tail_lines 行
      4. 找不到锚点就原样返回（宁可多留噪声，也不能把正文切没）
    """
    lines = (md or "").splitlines()
    if len(lines) <= 6:
        return md

    n = len(lines)
    is_prose = [_is_prose(l) for l in lines]

    anchor = None
    for i in range(n):
        if not is_prose[i]:
            continue
        run = sum(1 for j in range(i, min(n, i + BODY_WINDOW)) if is_prose[j])
        if run >= BODY_MIN_RUN:
            anchor = i
            break

    if anchor is None:
        return md

    start = anchor
    for j in range(anchor - 1, max(-1, anchor - 1 - back_lines), -1):
        if lines[j].strip().startswith("#"):
            start = j
            break

    end = len(lines)
    for k in range(n - 1, anchor, -1):
        if is_prose[k]:
            end = min(n, k + 1 + tail_lines)
            break

    body = lines[start:end]

    # 页脚截断。页脚常是一大片「纯文本」的组织名 / 栏目名清单 ——
    # 它们没有链接标记，躲过了链接密度判断，却塞满主题词：
    # 中华医学会的「友情链接」里列着运动医疗分会、肠外肠内营养学分会、
    # 心血管病学分会…… 一篇《报废固定资产处置公示》因此算出 0.42 的相关度
    # 被当成健康文章。这类样板只能按标记名确定性截断。
    cut = len(body)
    for i, ln in enumerate(body):
        if i == 0:
            continue
        head = re.sub(r"^[\s#>|\-*_·]+", "", ln)
        if FOOTER_MARKER.match(head):
            cut = i
            break
    body = body[:cut]

    return "\n".join(body).strip()


H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


def _first_h1(md: str, scan: int = 4000) -> str:
    """取 markdown 里的第一个一级标题，作为「文章标题」的备选。"""
    m = H1_RE.search((md or "")[:scan])
    if not m:
        return ""
    t = MD_LINK_LOCAL.sub(lambda x: x.group(0).split("](", 1)[0].lstrip("["), m.group(1))
    return re.sub(r"\s+", " ", t).strip()[:300]


def _better_title(meta_title: str, h1: str) -> str:
    """meta 标题和正文 H1 二选一。

    规则：H1 更长就用 H1。政府 CMS 的 meta 标题往往就是站点名（短），
    而正文 H1 才是真标题；正规媒体的 meta 标题通常是「文章标题 | 站点名」，
    比 H1 长，保持不动。
    """
    if h1 and len(h1) > len(meta_title):
        return h1
    return meta_title


def _pick_date(meta: dict) -> str | None:
    for k in ("published_time", "article:published_time", "date", "pubdate", "og:published_time"):
        v = meta.get(k)
        if v:
            return str(v)[:32]
    return None


# ------------------------------------------------------------------ httpx 降级后端
class SimpleBackend:
    """没有 Playwright 时的兜底：httpx 拉 HTML，正则去标签。

    抓不了 JS 渲染站点，但快、依赖少、不会被浏览器下载卡住。
    """

    name = "httpx-fallback"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._client = None
        self._last_hit: dict[str, float] = {}

    async def __aenter__(self):
        import httpx

        self._client = httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=BROWSER_HEADERS,
        )
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_hit.get(host, 0.0)
        wait = POLITE_DELAY - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_hit[host] = time.time()

    async def fetch(self, url: str) -> Fetched:
        await self._throttle(url)
        try:
            r = await self._client.get(url)
        except Exception as e:  # noqa: BLE001
            return Fetched(url=url, error=f"{type(e).__name__}: {e}")

        if r.status_code >= 400:
            return Fetched(url=url, error=f"HTTP {r.status_code}")

        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return Fetched(url=url, error=f"非 HTML: {ctype}")

        html = r.text
        title = _regex_title(html)
        body = html_to_text(html)
        links = _regex_links(html, url)

        return Fetched(
            url=url, ok=True, title=title, html_title=title,
            fit_markdown=body, markdown=body,
            published_at=_regex_date(html), links=links,
            meta={"title": title},
        )


TAG_RE = re.compile(r"<(script|style|nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>",
                    re.I | re.S)
BLOCK_RE = re.compile(r"</(p|div|h[1-6]|li|tr|section|article|br)\s*>", re.I)
TAG_ANY = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
NL_RE = re.compile(r"\n{3,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META_DATE_RE = re.compile(
    r'(?:published_time|publishdate|pubdate|date)["\']?\s*[:=]\s*["\'](\d{4}-\d{1,2}-\d{1,2})', re.I)
HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\'#]+)["\']', re.I)


def html_to_text(html: str) -> str:
    import html as html_mod

    s = TAG_RE.sub(" ", html or "")
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = BLOCK_RE.sub("\n", s)
    s = TAG_ANY.sub(" ", s)
    s = html_mod.unescape(s)
    s = WS_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    return NL_RE.sub("\n\n", s).strip()


def _regex_title(html: str) -> str:
    import html as html_mod

    m = TITLE_RE.search(html or "")
    return html_mod.unescape(WS_RE.sub(" ", m.group(1))).strip() if m else ""


def _regex_date(html: str) -> str | None:
    m = META_DATE_RE.search(html or "")
    return m.group(1) if m else None


def _regex_links(html: str, base: str) -> list[str]:
    out = []
    for href in HREF_RE.findall(html or ""):
        if href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        out.append(normalize_url(href, base))
    return out


# ------------------------------------------------------------------ 统一入口
async def open_backend(prefer: str = "crawl4ai"):
    """按可用性挑后端，返回 (backend, 说明)。"""
    if prefer == "crawl4ai" and Crawl4AIBackend.available():
        return Crawl4AIBackend(), "crawl4ai（真实浏览器渲染）"
    return SimpleBackend(), "httpx 降级（无 JS 渲染）"


def _base_dir(list_url: str) -> str:
    """列表页自身所在目录，用来判断候选是否同栏目。

    /n315/index.html          → /n315/
    /physical-activity-basics/ → /physical-activity-basics/
    /blog                      → /blog/
    /                          → /
    """
    p = urllib.parse.urlsplit(list_url).path or "/"
    if re.search(r"/(index|default|list|main)\.(html?|php|jsp|aspx?)$", p, re.I):
        p = p.rsplit("/", 1)[0]
    if not p.endswith("/"):
        p += "/"
    return p


async def discover(backend, list_url: str, domain: str, max_links: int = 40) -> list[str]:
    """从列表页/栏目页发现同域文章链接，按「像文章的程度」排序后取前 N 条。

    不做纯 DOM 顺序截断（那样只会拿到页顶导航），而是先过滤掉不像文章
    的链接，再按 article_rank 排序。同栏目的详情页会排在最前。
    """
    res = await backend.fetch(list_url)
    if not res.ok:
        return []

    base_dir = _base_dir(list_url)
    seen: set[str] = set()
    cands: list[str] = []
    for href in res.links:
        u = normalize_url(href, list_url)
        if u in seen or not same_site(u, domain) or not is_article_like(u):
            continue
        seen.add(u)
        cands.append(u)

    # 稳定排序：同分保持原 DOM 顺序（通常越靠前越新）
    cands.sort(key=lambda u: -article_rank(u, base_dir))
    return cands[:max_links]
