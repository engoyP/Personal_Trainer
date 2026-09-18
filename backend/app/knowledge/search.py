"""关键词搜索：用博查（Bochaai）Web Search API 按关键词发现全网新源。

为什么不用直接抓搜索引擎结果页：实测 Bing 返回缓存/无关内容、百度只给自家
m.content 页、搜狗直接反爬拦截，都不可靠。博查是国内面向 LLM 的搜索 API，
返回结构化 JSON，稳定且零解析成本。

**质量策略**（实测过：只搜一遍命中一堆门户/健康站，内容浅）：
  - 多路搜索：普通 + `site:gov.cn` + `site:nhs.uk` 权威域限定，把权威内容捞进来；
  - 合并去重后**按域名权威分降序**，权威的排前面（爬取按顺序取，等于优先抓权威）；
  - 低质域名黑名单（文库/文档站/内容农场）直接剔除。

没配 key 时优雅降级：返回空 + 提示，不抛异常（爬关键词是增强，不是主流程依赖）。
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.knowledge.scoring import domain_score

API_URL = "https://api.bochaai.com/v1/web-search"

# 低质/抓不到正文的站点：文库、文档分享站、内容农场。它们的正文是文档预览（需 JS
# 渲染或付费），抓到也是 not_article，白占抓取配额。实测搜「拉伸姿势教学」命中一堆
# 豆丁/原创力文档/学科网，3 篇全被判 not_article 拒收。
_BLOCK_HOSTS = (
    "docin.com", "book118.com", "wenku.baidu.com", "zxxk.com", "doc88.com",
    "360doc.com", "doc88.net", "51wendang.com", "renrendoc.com", "taodocs.com",
    "mayiwenku.com", "jinchutou.com", "xueshu.baidu.com",
    # 内容农场 / 图库 / 招聘工商站（实测搜饮食知识时混入的）
    "doczj.com", "nipic.com", "528045.com", "zhipin.com", "qcc.com",
    "aiqicha.baidu.com", "zhaopin.com", "qichamao.com",
)

# 权威域限定：多搜这几路，把政府/国际权威机构的内容捞进来（普通搜索几乎只会给门户）。
# 每加一个就多一次 API 调用，别贪多。
_AUTHORITY_SITES = ("gov.cn", "nhs.uk", "who.int")


def _blocked(url: str) -> bool:
    host = (httpx.URL(url).host or "").lower()
    return any(b in host for b in _BLOCK_HOSTS)


def available() -> bool:
    return bool((settings.BOCHA_API_KEY or "").strip())


def _one_pass(query: str, count: int) -> list[dict]:
    payload = {"query": query, "freshness": "noLimit", "summary": True,
               "count": max(1, min(count, 50))}
    headers = {"Authorization": f"Bearer {settings.BOCHA_API_KEY.strip()}",
               "Content-Type": "application/json"}
    r = httpx.post(API_URL, json=payload, headers=headers, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return (((data or {}).get("data") or {}).get("webPages") or {}).get("value") or []


def search_web(keyword: str, count: int = 15,
               authority_passes: bool = True,
               min_authority: float = 0.0) -> tuple[list[str], str]:
    """按关键词搜全网，返回 (URL 列表, 备注)。失败返回 ([], 原因)。

    结果按域名权威分降序 —— 爬取按顺序取名额，排序等于让权威内容优先。
    min_authority > 0.5 时只保留达到该权威分的结果（默认 0 = 不过滤，因为权威
    机构很少写「食材替换」这类生活内容，卡太死会一条都不剩）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "关键词为空"
    if not available():
        return [], "未配置 BOCHA_API_KEY（去 open.bochaai.com 注册后在 backend/.env 里填）"

    passes = [(keyword, count)]
    if authority_passes:
        per = max(3, count // 2)
        passes += [(f"{keyword} site:{s}", per) for s in _AUTHORITY_SITES]

    pages: list[dict] = []
    err = ""
    for q, n in passes:
        try:
            pages += _one_pass(q, n)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}"
    if not pages:
        return [], f"搜索失败：{err or '无返回'}"

    seen: set[str] = set()
    rows: list[tuple[float, str]] = []
    blocked = 0
    low = 0
    for p in pages:
        u = (p.get("url") or "").strip()
        if not u or u in seen:
            continue
        if _blocked(u):
            blocked += 1
            continue
        seen.add(u)
        sc = domain_score(httpx.URL(u).host or "")
        if min_authority and sc < min_authority:
            low += 1
            continue
        rows.append((sc, u))

    if not rows:
        hint = f"（过滤 {blocked} 条低质站"
        if low:
            hint += f"，{low} 条低于权威门槛 {min_authority}"
        return [], f"搜索无有效结果{hint}）"

    rows.sort(key=lambda x: -x[0])
    out = [u for _, u in rows]
    auth = sum(1 for s, _ in rows if s >= 0.85)
    note = f"博查命中 {len(out)} 条（权威源 {auth} 条）"
    if blocked:
        note += f"，过滤 {blocked} 条低质站"
    if low:
        note += f"，{low} 条低于权威门槛"
    return out, note
