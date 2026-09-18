"""打分：域名权威 + 时效 + 主题相关度 → 综合分。

设计取舍：
- 权威分权重最高（0.55）。知识库里最怕的是「听起来对但没依据」的内容，
  宁可少收几篇，也不能让自媒体文章和 ACSM 指南平起平坐。
- 时效只占 0.20。运动科学的底层结论（渐进超负荷、80/20 强度分配）
  十年二十年都不变，不该因为文章旧了就沉底。真正的衰减只惩罚「过时结论」。
- 相关度占 0.25，用来把同一站点里跑题的文章（招生简章、会议通知）滤掉。
"""
from __future__ import annotations

import math
import re
from datetime import date

from app.knowledge.sources import TOPIC_KEYWORDS, domain_score

W_DOMAIN = 0.55
W_RECENCY = 0.20
W_RELEVANCE = 0.25

# 时效半衰期（天）。指南类文档不衰减。
HALF_LIFE_DAYS = 900
GUIDELINE_FLOOR = 0.75  # 无发布日期但来源权威时的保底时效分

# 分类/相关度的计算窗口。
#
# 第一版这里是 2500，理由是「正文开头的论点密度最高」—— 实测证明这是错的：
#   · ACSM 一篇运动科学文章，「exercise」全文出现 21 次，但第一次出现在
#     第 2500 字之后（前面是侧栏推广块），截断后相关度算成 0.00 被误杀
#   · CDC 用的是 "physical activity" 而不是 "exercise"，命中本来就稀疏
# 相关性判断要的是「全文有没有在讲这件事」，不是「开头密不密」。
# 这里是纯本地字符串计算，不花 token，所以直接放到 20000 —— 覆盖几乎
# 所有文章的长度，同时避免个别 20 万字的页面拖慢批处理。
CLASSIFY_CHARS = 20000


def _prose_only(text: str, link_share_cap: float = 0.40) -> str:
    """只保留「正文行」，丢掉导航行。

    为什么必须做这一步：导航菜单里全是主题词 —— 一个医学会首页的页眉页脚
    写着「健康 / 营养 / 训练 / 康复」，一篇《报废固定资产处置公示》也能因此
    算出 0.42 的相关度，被当成健康文章入库。相关度必须在**散文**上算，
    不能在菜单标签上算。

    规则：整行链接占比超过上限的直接丢（那是导航），行内链替换成锚文本。
    """
    out: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        links = list(MD_LINK_RE.finditer(s))
        linked = sum(len(m.group(0)) for m in links)
        if linked / max(1, len(s)) > link_share_cap:
            continue
        if links:
            s = MD_LINK_RE.sub(lambda m: m.group(0).split("](", 1)[0].lstrip("["), s)
        out.append(s)
    return "\n".join(out)


def classify_blob(text: str, title: str = "") -> str:
    """拼出用于分类/相关度判断的文本（标题 + 正文）。

    正文先剔除导航行，否则菜单里的主题词会把跑题页面顶过关卡。
    """
    body = _prose_only((text or ""))[:CLASSIFY_CHARS]
    return f"{title}\n{body}"


_blob = classify_blob  # 内部旧名字，保留兼容


# Markdown 链接。crawl4ai 输出的是 markdown，链接结构清晰可数。
MD_LINK_RE = re.compile(r"\[[^\]]{0,120}\]\([^)\s]{0,300}\)")


def link_ratio(text: str) -> float:
    """链接字符占全文的比例。导航页/索引页会很高。"""
    t = text or ""
    if not t:
        return 1.0
    linked = sum(len(m.group(0)) for m in MD_LINK_RE.finditer(t))
    return round(linked / len(t), 3)


def is_index_like(text: str, threshold: float = 0.80) -> bool:
    """像不像「几乎全是链接的目录页」。

    阈值是量出来的，不是拍的。实测（crawl4ai markdown，_clean_md 之后）：

        真文章   ACSM 0.495 / CDC 0.673 / CDC 0.714
        索引页   CDC site index 0.849
        列表页   ACSM /blog 0.459 ← 比真文章还低！

    结论：链接占比**不能**当主判据。列表页的链接文字很短，占比反而低；
    而正规文章因为带引用和正文内链，占比不低。任何 0.4~0.5 的阈值都会误杀真文章。

    所以这里只留一个很松的 0.80 做预筛 —— 只抓「几乎没有任何正文」的极端页面，
    省下一次 LLM 调用。真正的文章/非文章判断交给 LLM 的 substantive 字段，
    它读过正文，比任何字符统计都准（实测它能准确指出「本页仅含导航与 Cookie 声明，
    无实质论述」这种话，而链接统计完全看不出来）。
    """
    return link_ratio(text) >= threshold


def recency_score(published_at: date | None, evidence_level: str = "unknown",
                  today: date | None = None) -> float:
    """时效分：越新越高，指数衰减。指南类给保底。"""
    if evidence_level in ("guideline", "review"):
        return max(GUIDELINE_FLOOR, 0.9)
    if not published_at:
        return 0.6  # 拿不到日期，中性偏低，不冤
    today = today or date.today()
    age = max(0, (today - published_at).days)
    return round(math.exp(-age * math.log(2) / HALF_LIFE_DAYS), 3)


def _keyword_hits(text: str) -> dict[str, int]:
    # 连字符统一成空格再匹配。
    # 否则 "Weight-loss drug" 匹配不上词表里的 "weight loss"，
    # "physical-activity" 也匹配不上 "physical activity" —— 而英文站点
    # 在标题和 URL 里用连字符是常态。
    low = (text or "").lower().replace("-", " ")
    hits: dict[str, int] = {}
    for topic, words in TOPIC_KEYWORDS.items():
        n = 0
        for w in words:
            key = w.lower().replace("-", " ")
            # 英文词按词边界匹配，避免 "rest" 命中 "restaurant"
            if re.search(r"[a-z]", key):
                if re.search(rf"\b{re.escape(key)}\b", low):
                    n += 1
            elif key in low:
                n += 1
        if n:
            hits[topic] = n
    return hits


# 相关度归一的「满分命中数」。
# 命中数统计的是「命中了多少个不同的关键词」，不是出现次数 ——
# 所以它天然与文档长度弱相关，不需要按长度缩放。
# 实测：正规运动文章命中 8-25 个，边缘内容（Cookie 页、社会照护指南）1-3 个。
SATURATION_HITS = 12


def classify(text: str, fallback: str = "training") -> tuple[str, list[str], float]:
    """返回 (主分类, 命中主题列表, 相关度分)。"""
    hits = _keyword_hits(text)
    if not hits:
        return fallback, [], 0.0  # 一个关键词都不沾，基本可以判跑题

    topics = sorted(hits, key=lambda k: -hits[k])
    total = sum(hits.values())
    relevance = round(min(1.0, total / SATURATION_HITS), 3)
    return topics[0], topics, relevance


def evidence_level_of(domain: str, text: str, title: str = "") -> str:
    """粗判证据等级。用于时效保底和前端标注。"""
    blob = f"{title}\n{text[:2000]}".lower()
    if any(k in blob for k in ("指南", "guideline", "recommendation", "position stand", "共识")):
        return "guideline"
    if any(k in blob for k in ("systematic review", "meta-analysis", "系统评价", "荟萃分析")):
        return "review"
    if any(k in blob for k in ("randomized", "randomised", "对照试验", "rct", "clinical trial")):
        return "rct"
    if any(h in domain for h in ("pubmed", "ncbi", "bmj", "cochrane", "springeropen")):
        return "review"
    return "popular"


def combined_score(domain: str, published_at: date | None, text: str,
                   title: str = "", evidence_level: str = "unknown",
                   source_authority: float | None = None,
                   today: date | None = None) -> dict:
    """算出一篇文章的全部得分与分类，返回可直接落库的 dict。"""
    cat, topics, relevance = classify(_blob(text, title))
    domain_s = domain_score(domain, source_authority)
    ev = evidence_level if evidence_level != "unknown" else evidence_level_of(domain, _blob(text, title))
    recency_s = recency_score(published_at, ev, today)

    score = W_DOMAIN * domain_s + W_RECENCY * recency_s + W_RELEVANCE * relevance

    return {
        "category": cat,
        "topics": topics,
        "evidence_level": ev,
        "domain_score": round(domain_s, 3),
        "recency_score": round(recency_s, 3),
        "relevance_score": round(relevance, 3),
        "score": round(score, 4),
    }


# 机构新闻 / 行政事务。这类页面是「真文章」，相关度也过闸门
# （标题里带「营养」「健康」就能命中关键词），但它是《报废固定资产处置公示》
# 《继续医学教育项目通知》这种内容，进知识库只会污染检索结果。
# 中文权威站的列表页里这类占比极高，必须按标题形态确定性挡掉。
NEWS_NOISE = re.compile(
    r"("
    r"会议|大会|召开|举办|开班|座谈|研讨|交流|论坛|峰会|沙龙|讲座|报告会|"
    r"调研|视察|考察|来访|访问|签署|签约|揭牌|启动仪式|动员|换届|述职|"
    r"表彰|颁奖|联欢|党日|党建|党课|主题教育|巡视|督查|通报|公示|公告|通知|"
    r"中标|成交|招标|采购|询价|招聘|任免|聘期|培训班|研修班|学习班|征文|征稿|"
    r"申报|评审结果|评审通知|年检|备案|注销|资产处置|决算|预算|审计|"
    r"工作动态|工作纪实|活动纪实"
    r")"
)


def is_news_noise(title: str) -> bool:
    """标题像不像机构新闻/行政事务（而非知识文章）。"""
    return bool(NEWS_NOISE.search(title or ""))


def is_relevant(text: str, title: str = "", min_relevance: float = 0.25) -> bool:
    """入库前的主题闸门。跑题的文章再权威也别收——知识库要能用来回答问题。

    阈值 0.25 对应「命中 3 个不同主题关键词」。这是**粗筛**，不是终审：
    它的职责是把 Cookie 声明、社会照护指南这类一眼无关的页面挡在 LLM 调用之前
    （实测 0.08-0.17），而把「是不是真文章」的细活交给读过正文的 LLM
    （extract 的 substantive 字段）。
    调到 0.35 会把 CDC「physical activity」这类只用一种措辞的页面误杀。
    """
    _, _, rel = classify(classify_blob(text, title))
    return rel >= min_relevance
