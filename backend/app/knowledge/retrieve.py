"""检索：给教练对话和计划生成提供「循证依据」。

检索只用摘要和要点，不碰全文 —— 和「LLM 看不到原始采样点」是同一条纪律：
把几千字的文章塞进 prompt，既烧 token 又会稀释掉真正该看的上下文。
需要原文时前端单独展开，不进模型。

**两条路径，语义优先**：
  1. 语义路径（本地 Qwen3-Embedding 召回 + Qwen3-Reranker 精排）——
     跨语言能力是真需求：用户用中文提问，知识库里一半是英文指南，
     关键词匹配只能靠手工词典桥接，语义模型是天然对齐的。
  2. 关键词路径 —— 语义服务没起、文章没算过向量、或重排不可用时自动退回。
     这条路径必须一直可用：语义能力是增强，不是依赖。

两条路径的共同点：都掺入**文章权威分**。同样命中「间歇训练」，
ACSM 的指南应该排在自媒体文章前面。
"""
from __future__ import annotations

import math
import re

from sqlalchemy.orm import Session

from app.knowledge import semantics
from app.knowledge.sources import TOPIC_KEYWORDS
from app.models.knowledge import Article

W_MATCH = 0.60
W_AUTHORITY = 0.40

# 语义路径里的权重：以重排分为主，权威分只做同分时的次序
W_SEM = 0.70
W_SEM_AUTH = 0.30
# 向量覆盖率低于这个比例就不走语义路径（宁可全走关键词，也不要结果忽多忽少）
MIN_EMBED_COVERAGE = 0.5
# 召回补齐：关键词匹配分达到这个线的文章，额外并进召回池（见 _recall_pool）
KW_RECALL_MIN = 0.3
# 每篇文章最多拿几条要点去单独打分。要点级的打分是「取最大」，多喂几条只是
# 多几个候选、不会拉低分数，所以这里不算严格上限 —— 它只为控制延迟。
# 取 8 是因为抽取侧最多也就产出 8 条，等于全用上：实测第 6 条往后才是
# 真正对题的那条（《Sleep and Immunity》的睡眠卫生建议），砍掉就白拆了。
MAX_POINT_PROBES = 8

# 中文停用词，避免「我」「的」「怎么」这种把分数刷上去
STOP = set("我 你 他 她 它 的 了 是 在 和 与 或 有 没 不 很 太 都 也 还 就 要 会 能 可以 "
           "怎么 如何 什么 为什么 哪些 哪个 这个 那个 这样 那样 应该 需 需要 想 请 帮 "
           "一下 一些 时候 现在 最近 今天 昨天 明天 吗 呢 啊 吧 嘛 the a an of to in for "
           "and or is are be do does how what why which i my me you your".split())

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
EN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def query_terms(text: str, max_terms: int = 80) -> set[str]:
    """把查询拆成检索词，并做一次中英桥接。

    两个坑：
    1. 中文没有分词器，用 2-4 字 n-gram 做近似（够用）
    2. **跨语言**：用户用中文提问，而知识库里大量文章是英文的。
       光靠字面匹配，「减脂期间饮食怎么吃」永远匹配不到
       "weight loss" / "protein"。所以这里用 TOPIC_KEYWORDS 当词典：
       查询命中某个主题的中文词，就把该主题的英文词一并加进来。
       不用额外装翻译模型，利用的是已有的领域词表。
    """
    t = (text or "").lower().replace("-", " ")
    terms: set[str] = set()

    for w in EN_RE.findall(t):
        if w not in STOP:
            terms.add(w)

    for run in CJK_RE.findall(t):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g not in STOP:
                    terms.add(g)

    # 中英桥接：命中中文主题词 → 补上同主题的英文词
    for _topic, words in TOPIC_KEYWORDS.items():
        zh = [w for w in words if not re.search(r"[a-zA-Z]", w)]
        en = [w for w in words if re.search(r"[a-zA-Z]", w)]
        if any(w in t for w in zh):
            terms.update(w.lower().replace("-", " ") for w in en)

    return set(list(terms)[:max_terms])


def _searchable(a: Article) -> str:
    """拼出用于匹配的文本，连字符统一成空格（和 query_terms 的处理保持一致）。"""
    parts = [a.title or "", a.summary or "", a.organization or ""]
    parts += [str(x) for x in (a.key_points or [])]
    parts += [str(x) for x in (a.topics or [])]
    return " ".join(parts).lower().replace("-", " ")


def _match_score(article_text: str, terms: set[str], title_text: str = "") -> float:
    if not terms:
        return 0.0
    hit = 0.0
    for term in terms:
        if term in title_text:
            hit += 3.0          # 标题命中权重高
        elif term in article_text:
            hit += 1.0
    # 归一到 0-1，命中 8 个词就算强相关
    return min(1.0, hit / 8.0)


def _article_parts(a: Article) -> list[str]:
    """把一篇文章拆成可**独立打分**的片段：就是它的各条要点。

    重排是「(查询, 段落) → 一个分数」的交叉编码器，整段喂进去时分数被整段的
    主话题主导。实测《Sleep and Immunity》六条要点里五条讲免疫/疫苗/炎症、
    只有一条讲睡眠卫生，问「怎么提高睡眠质量」时整段只有 -6.02（被阈值挡掉），
    而单独打那一条睡眠卫生要点是 +4.34（排第一）。一篇文章只要**任何一个片段**
    能回答查询就该判为相关 —— 所以拆开打分、取最大值。

    要点天生是自足论断，适合当检索单元。这里**不把摘要也当片段**：摘要有
    1000+ 字，重排会把它和短要点 padding 到同一长度，实测能把一次查询从
    1 秒拖到 9 秒，而它承载的信息本来就是各条要点的综合。只有摘要要点都空
    （抽取降级过）时才退回整段文本。
    """
    parts = [str(p).strip() for p in (a.key_points or [])[:MAX_POINT_PROBES]]
    parts = [p for p in parts if p]
    if not parts:
        parts = [semantics.rerank_text(a.title, a.summary, a.key_points, a.content_md)]
    return parts


def _probe_ranks(query: str, pool: list[tuple[float, Article]]) -> list[float] | None:
    """对召回池做「要点级」重排，返回每篇文章的最大分。None 表示不可用。

    扁平化后一次批量送进模型服务（服务内部分批），比逐篇调用少很多往返。
    """
    probes: list[str] = []
    owner: list[int] = []
    for i, (_, a) in enumerate(pool):
        for text in _article_parts(a):
            probes.append(text)
            owner.append(i)
    if not probes:
        return None
    ranks = semantics.rerank(query, probes)
    if ranks is None or len(ranks) != len(probes):
        return None
    best: list[float] = [float("-inf")] * len(pool)
    for i, r in zip(owner, ranks):
        if r > best[i]:
            best[i] = r
    return best


def _row(a: Article, *, total: float, match: float,
         extra: dict | None = None) -> dict:
    row = {
        "id": a.id,
        "title": a.title,
        "organization": a.organization,
        "url": a.url,
        "domain": a.domain,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "category": a.category,
        "evidence_level": a.evidence_level,
        "summary": a.summary,
        "key_points": a.key_points or [],
        "score": round(total, 4),
        "match": round(match, 3),
        "authority": a.domain_score,
    }
    if extra:
        row.update(extra)
    return row


def _recall_pool(query: str, scored: list[tuple[float, Article]]) -> list[tuple[float, Article]]:
    """取召回候选：语义余弦 top-N 打底，再把关键词命中的文章并进来。

    为什么不能只看余弦：中文口语化提问（「减脂期怎么做有氧」）和文章措辞对不上时，
    真正对题的文章可能掉到 30 名开外，重排根本没机会看到它 —— 修 instruction 之前
    实测就是「目标文章排 51/75」，一路修到召回池之外。关键词那路有中英桥接词典兜底，
    补进池子后统一交给重排裁决；补进来的不达标照样被阈值挡掉，不会因此混过闸门。
    """
    size = semantics.RECALL_POOL
    pool = list(scored[:size])
    seen = {a.id for _, a in pool}
    terms = query_terms(query)
    if not terms:
        return pool
    extra: list[tuple[float, Article]] = []
    for cos, a in scored:                       # scored 仍按余弦序，补位的也保持这个次序
        if a.id in seen:
            continue
        m = _match_score(_searchable(a), terms, (a.title or "").lower().replace("-", " "))
        if m >= KW_RECALL_MIN:
            extra.append((cos, a))
    return pool + extra[: max(1, size // 3)]


def _semantic_retrieve(query: str, articles: list[Article],
                       top_n: int) -> list[dict] | None:
    """向量召回 → 重排精排。返回 None 表示语义路径不可用，交给关键词路径。"""
    with_vec = [a for a in articles if a.embedding]
    if not with_vec or len(with_vec) < len(articles) * MIN_EMBED_COVERAGE:
        return None

    qv = semantics.embed([query], query=True)
    if not qv:
        return None
    qvec = qv[0]

    scored: list[tuple[float, Article]] = []
    for a in with_vec:
        v = semantics.unpack(a.embedding)
        if not v:
            continue
        scored.append((semantics.cosine(qvec, v), a))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])

    pool = _recall_pool(query, scored)
    best = _probe_ranks(query, pool)
    if best is None:
        return None

    keep = [(best[i], cos, a) for i, (cos, a) in enumerate(pool)
            if best[i] >= semantics.RERANK_KEEP]
    if not keep:
        # 重排后一条都不达标 → 宁可返回空，也不拿擦边内容当「循证依据」
        return []

    rows: list[tuple[float, dict]] = []
    for r, cos, a in keep:
        # 重排原始分（yes/no logit 差）映射到 0-1。以 0 为中心：阈值已经先把
        # 明显不相关的挡在外面（RERANK_KEEP=-1），这里只负责把留下来的排开次序。
        rel = 1.0 / (1.0 + math.exp(-r))
        total = W_SEM * rel + W_SEM_AUTH * (a.score or 0.5)
        rows.append((total, _row(a, total=total, match=rel,
                                 extra={"rerank": round(r, 3), "cosine": round(cos, 4),
                                        "via": "semantic"})))
    rows.sort(key=lambda x: -x[0])
    return [r for _, r in rows[:top_n]]


def _keyword_retrieve(query: str, articles: list[Article], top_n: int,
                      min_match: float) -> list[dict]:
    terms = query_terms(query)
    ranked: list[tuple[float, Article, float]] = []
    for a in articles:
        text = _searchable(a)
        m = _match_score(text, terms, (a.title or "").lower().replace("-", " "))
        if m < min_match:
            continue
        total = W_MATCH * m + W_AUTHORITY * (a.score or 0.5)
        ranked.append((total, a, m))

    ranked.sort(key=lambda x: -x[0])
    return [_row(a, total=total, match=m, extra={"via": "keyword"})
            for total, a, m in ranked[:top_n]]


def retrieve(db: Session, query: str, top_n: int = 6,
             category: str | None = None, min_score: float = 0.0,
             only_authoritative: bool = False,
             min_match: float = 0.2) -> list[dict]:
    """按查询返回最相关的文章（含摘要与要点，不含全文）。

    语义路径优先，不可用时退回关键词。两条路径都必须**宁缺毋滥**：
    实测过不设闸门的后果 —— 问「减脂期间饮食怎么吃」，捞回来的是
    「睾酮治疗与心脏风险」。把这种文章标成「循证依据」喂给教练，
    比不给依据更糟：模型会顺着它编出有出处的错误建议。
    返回空列表时教练会按自己的专业知识回答，这是更安全的降级。
    """
    q = db.query(Article).filter(Article.status == "ok")
    if category:
        q = q.filter(Article.category == category)
    if min_score > 0:
        q = q.filter(Article.score >= min_score)
    if only_authoritative:
        q = q.filter(Article.domain_score >= 0.85)

    articles = q.all()
    if not articles:
        return []

    got = _semantic_retrieve(query, articles, top_n)
    if got is not None:
        return got
    return _keyword_retrieve(query, articles, top_n, min_match)


def format_evidence(articles: list[dict], max_points: int = 3) -> str:
    """把检索结果渲染成 prompt 里的一段「循证依据」。

    刻意保留出处（机构 + 年份）：让模型知道这是有出处的结论，
    而不是让它自己去编。同时明确要求「有冲突时以本人数据为准」。
    """
    if not articles:
        return "（知识库暂无相关条目）"

    lines: list[str] = []
    for i, a in enumerate(articles, 1):
        year = (a.get("published_at") or "")[:4]
        org = a.get("organization") or a.get("domain") or "未知来源"
        head = f"[{i}] {a.get('title')}  —— {org}{' ' + year if year else ''}"
        lines.append(head)
        if a.get("summary"):
            lines.append(f"    摘要：{a['summary']}")
        pts = (a.get("key_points") or [])[:max_points]
        if pts:
            lines.append("    要点：" + "；".join(str(p) for p in pts))
    return "\n".join(lines)


EVIDENCE_RULES = """引用知识库时的纪律：
- 上面「循证依据」是真实检索到的资料，只能引用其中确实写了的内容，不要编造出处、页码或数据。
- 有依据支撑的建议，在解释里用 [1][2] 这样的编号标出来源。
- 如果知识库内容与用户的实测数据冲突，以用户自己的数据为准，并说明这个差异。
- 知识库没有覆盖到的地方，就按你自己的专业知识回答，不要假装有依据。"""
