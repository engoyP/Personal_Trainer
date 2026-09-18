"""入库：去重、打标、写库。

去重按规范化 URL 的哈希。同一篇文章被改写过（标题变了、正文更新）时更新
原记录而不是新增一条 —— 知识库要的是「一个论点一份出处」，
不是同一份指南的五个版本互相稀释检索结果。
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy.orm import Session

from app.knowledge.extract import extract
from app.knowledge.scoring import (
    combined_score, is_index_like, is_news_noise, is_relevant,
)
from app.models.knowledge import Article

# 低于这个字数基本不是文章（导航页、404、图片页）
MIN_CONTENT_CHARS = 300
# 正文超过这个长度才截断存储，避免个别页面几 MB 撑爆库
MAX_STORE_CHARS = 200_000


def url_hash(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:32]


def _clean_md(text: str) -> str:
    """crawl4ai 的 fit_markdown 里常留着一堆连续链接列表（导航、相关阅读），压一压。"""
    t = text or ""
    t = re.sub(r"\n{3,}", "\n\n", t)
    # 连续 3 行以上都像短链接行 → 折叠
    lines = t.splitlines()
    out: list[str] = []
    link_run = 0
    for ln in lines:
        s = ln.strip()
        is_linkish = bool(re.match(r"^[-*\[\]()!>\s]*(https?://|\[.*\]\().*$", s)) or (
            len(s) < 40 and s.count("](") > 0
        )
        if is_linkish:
            link_run += 1
            if link_run > 3:
                continue
        else:
            link_run = 0
        out.append(ln)
    return "\n".join(out).strip()


def save_article(db: Session, *, url: str, domain: str, raw_text: str,
                 title: str, fallback_date=None, source_id: int | None = None,
                 lang: str = "zh", source_authority: float | None = None,
                 do_extract: bool = True, extracted: dict | None = None) -> tuple[str, Article | None]:
    """把一篇文章落库。

    extracted 由调用方预先算好时（并发抽取场景）直接传入，避免在这里再调一次 LLM。

    返回 (action, article)，
    action ∈ inserted / updated / skipped / too_short / index_page /
             news_item / irrelevant / not_article。
    """
    content = _clean_md(raw_text)
    if len(content) < MIN_CONTENT_CHARS:
        return "too_short", None

    # 闸门一：目录页/索引页。链接密度高的页面关键词命中率虚高，必须提前挡掉。
    if is_index_like(content):
        return "index_page", None

    # 闸门二：机构新闻/行政事务。放在相关度之前 ——
    # 中文权威站的列表页里这类内容占比极高，而且标题带「营养」「健康」
    # 就能骗过相关度闸门（《报废固定资产处置公示》算出 0.42）。
    # 提前挡掉还能省下一次 LLM 调用。
    if is_news_noise(title):
        return "news_item", None

    h = url_hash(url)
    existing = db.query(Article).filter(Article.url_hash == h).first()

    # 闸门三：主题相关度。跑题的文章再权威也不收，除非已经在库里（不主动删）
    if existing is None and not is_relevant(content, title):
        return "irrelevant", None

    fields: dict = {}
    if extracted is not None:
        fields = extracted
    elif do_extract:
        fields = extract(title, content, fallback_date=fallback_date, domain=domain)
    else:
        fields = {
            "title": title, "organization": "", "author": "",
            "published_at": None, "category": "training", "topics": [],
            "summary": "", "key_points": [], "evidence_level": "unknown",
        }

    # 闸门四：模型读了正文之后判定这不是文章页（JS 没渲染完、只剩导航框架等）。
    # 这个判断只有真正读了内容才做得出来，靠 URL 猜不出来。
    if fields.get("substantive") is False:
        return "not_article", None

    # 抽取后的标题再判一次新闻噪声 —— LLM 有时会还原出比页面标题更准确的标题
    if existing is None and is_news_noise(fields.get("title") or ""):
        return "news_item", None

    scoring = combined_score(
        domain=domain,
        published_at=fields.get("published_at"),
        text=content,
        title=fields.get("title") or title,
        evidence_level=fields.get("evidence_level") or "unknown",
        source_authority=source_authority,
    )

    if existing is not None:
        # 正文没变就不动，省掉一次写和 updated_at 抖动
        if existing.content_md == content and existing.title == fields["title"]:
            return "skipped", existing
        action = "updated"
        art = existing
    else:
        action = "inserted"
        art = Article(url=url, url_hash=h)
        db.add(art)

    art.domain = domain
    art.source_id = source_id
    art.lang = lang
    art.title = (fields.get("title") or title or "")[:500]
    art.organization = fields.get("organization") or ""
    art.author = fields.get("author") or ""
    art.published_at = fields.get("published_at")
    art.category = fields.get("category") or "training"
    art.topics = fields.get("topics") or []
    art.summary = fields.get("summary") or ""
    art.key_points = fields.get("key_points") or []
    art.evidence_level = fields.get("evidence_level") or "unknown"
    art.content_md = content[:MAX_STORE_CHARS]
    art.word_count = len(content)
    art.status = "ok"

    art.domain_score = scoring["domain_score"]
    art.recency_score = scoring["recency_score"]
    art.relevance_score = scoring["relevance_score"]
    art.score = scoring["score"]

    db.flush()
    return action, art


def purge_low_quality(db: Session, min_score: float = 0.0) -> int:
    """清掉跑题或过短的历史记录。默认不删，由调用方给阈值。"""
    if min_score <= 0:
        return 0
    n = db.query(Article).filter(Article.score < min_score).delete(synchronize_session=False)
    db.commit()
    return n


def stats(db: Session) -> dict:
    from sqlalchemy import func

    total = db.query(func.count(Article.id)).scalar() or 0
    rows = (db.query(Article.domain, func.count(Article.id))
            .group_by(Article.domain).order_by(func.count(Article.id).desc()).all())
    cats = (db.query(Article.category, func.count(Article.id))
            .group_by(Article.category).all())
    return {
        "total": total,
        "by_domain": [{"domain": d, "count": c} for d, c in rows[:20]],
        "by_category": [{"category": k, "count": v} for k, v in cats],
    }
