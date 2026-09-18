from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.knowledge import retrieve as kb_retrieve
from app.knowledge.store import stats as kb_stats
from app.models.knowledge import Article, CrawlRun, Source

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _article_brief(a: Article) -> dict:
    return {
        "id": a.id, "title": a.title, "url": a.url, "domain": a.domain,
        "organization": a.organization, "category": a.category,
        "lang": a.lang, "evidence_level": a.evidence_level,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "summary": a.summary, "key_points": a.key_points or [],
        "topics": a.topics or [], "score": a.score,
        "domain_score": a.domain_score, "recency_score": a.recency_score,
        "relevance_score": a.relevance_score,
        "word_count": a.word_count,
    }


@router.get("/articles")
def list_articles(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="关键词，标题/摘要/要点里搜"),
    category: str | None = None,
    domain: str | None = None,
    evidence_level: str | None = None,
    min_score: float | None = None,
    sort: str = Query("score", pattern="^(score|published|recent)$"),
    limit: int = Query(30, le=200),
    offset: int = 0,
):
    """文章列表。默认按综合分（权威+时效+相关度）排序。"""
    query = db.query(Article).filter(Article.status == "ok")

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Article.title.ilike(like),
            Article.summary.ilike(like),
            Article.organization.ilike(like),
            # key_points 是 JSON 列，转字符串后模糊匹配，SQLite/PG 都能用
            Article.key_points.cast(String).ilike(like),
        ))
    if category:
        query = query.filter(Article.category == category)
    if domain:
        query = query.filter(Article.domain == domain)
    if evidence_level:
        query = query.filter(Article.evidence_level == evidence_level)
    if min_score is not None:
        query = query.filter(Article.score >= min_score)

    total = query.count()

    if sort == "published":
        query = query.order_by(Article.published_at.desc().nullslast(), Article.score.desc())
    elif sort == "recent":
        query = query.order_by(Article.created_at.desc())
    else:
        query = query.order_by(Article.score.desc(), Article.published_at.desc().nullslast())

    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "items": [_article_brief(a) for a in rows]}


@router.get("/articles/{article_id}")
def get_article(article_id: int, db: Session = Depends(get_db)):
    a = db.get(Article, article_id)
    if not a:
        raise HTTPException(404, "文章不存在")
    data = _article_brief(a)
    data["content_md"] = a.content_md
    data["author"] = a.author
    return data


@router.get("/search")
def search(q: str, top_n: int = Query(6, le=20), db: Session = Depends(get_db)):
    """给教练用的检索接口：只回摘要与要点，不回全文。"""
    return {"items": kb_retrieve.retrieve(db, q, top_n=top_n)}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    from sqlalchemy import func

    base = kb_stats(db)
    base["by_evidence"] = [
        {"level": lv, "count": c} for lv, c in
        db.query(Article.evidence_level, func.count(Article.id))
          .group_by(Article.evidence_level).all()
    ]
    base["sources"] = [
        {
            "id": s.id, "name": s.name, "domain": s.domain, "lang": s.lang,
            "category": s.category, "authority": s.authority, "enabled": s.enabled,
            "last_crawled_at": s.last_crawled_at.isoformat() if s.last_crawled_at else None,
            "article_count": db.query(func.count(Article.id))
                               .filter(Article.domain == s.domain).scalar() or 0,
        }
        for s in db.query(Source).order_by(Source.authority.desc()).all()
    ]
    base["last_run"] = _run_brief(db.query(CrawlRun).order_by(CrawlRun.id.desc()).first())
    return base


def _run_brief(r: CrawlRun | None) -> dict | None:
    if not r:
        return None
    return {
        "id": r.id, "trigger": r.trigger, "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "discovered": r.discovered, "fetched": r.fetched,
        "inserted": r.inserted, "updated": r.updated,
        "skipped": r.skipped, "failed": r.failed,
        "error": r.error,
    }


@router.get("/runs")
def list_runs(limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    rows = db.query(CrawlRun).order_by(CrawlRun.id.desc()).limit(limit).all()
    return {"items": [_run_brief(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    r = db.get(CrawlRun, run_id)
    if not r:
        raise HTTPException(404, "爬取记录不存在")
    data = _run_brief(r)
    data["log"] = r.log or []
    return data


class CrawlIn(BaseModel):
    trigger: str = "manual"
    keyword: str = ""                    # 非空则按关键词搜全网（不用源清单）
    min_authority: float = 0.0           # 关键词搜索的域名权威门槛（0.85=只收权威源）
    max_per_source: int = 12
    only_domains: list[str] | None = None
    prefer_backend: str = "crawl4ai"     # crawl4ai / httpx


_crawl_state = {"running": False}


@router.post("/crawl")
def trigger_crawl(data: CrawlIn, background: BackgroundTasks):
    """手动触发一轮爬取。

    爬一轮要几分钟到十几分钟，接口不等着，直接丢后台跑，
    进度去 /runs 看。用同步 def 因为要跑 LangGraph 的同步 invoke。
    """
    if _crawl_state["running"]:
        raise HTTPException(409, "已有一轮爬取在进行中")

    def _job():
        from app.knowledge.graph import run_knowledge

        _crawl_state["running"] = True
        try:
            run_knowledge(trigger=data.trigger, keyword=data.keyword,
                          min_authority=data.min_authority,
                          max_per_source=data.max_per_source,
                          prefer_backend=data.prefer_backend,
                          only_domains=data.only_domains)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        finally:
            _crawl_state["running"] = False

    background.add_task(_job)
    msg = (f"已开始后台爬取（关键词「{data.keyword}」，搜全网）"
           if data.keyword.strip() else "已开始后台爬取")
    return {"ok": True, "message": msg + "，进度见 /api/knowledge/runs",
            "keyword": data.keyword, "max_per_source": data.max_per_source}


@router.get("/crawl/status")
def crawl_status():
    return {"running": _crawl_state["running"]}


class SourceIn(BaseModel):
    enabled: bool | None = None
    authority: float | None = None
    list_urls: list[str] | None = None


@router.put("/sources/{source_id}")
def update_source(source_id: int, data: SourceIn, db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404, "数据源不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True}


@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    rows = db.query(Article).filter(Article.status == "ok").all()
    from collections import Counter

    c = Counter(a.category for a in rows)
    labels = {
        "training": "训练方法", "nutrition": "营养补给", "health": "健康指标",
        "recovery": "恢复睡眠", "injury": "伤病防护", "weight_loss": "减脂控重",
    }
    return {"items": [{"key": k, "label": labels.get(k, k), "count": v}
                      for k, v in c.most_common()]}
