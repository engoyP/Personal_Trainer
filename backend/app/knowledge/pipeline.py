"""每周爬取流水线：按阶段拆开，每个阶段对应知识子图的一个节点。

分段的理由：每段瓶颈不同，调优和重试策略也不一样。
  1. 发现（网络 IO，轻）—— 并发，只抓列表页
  2. 抓取（网络 IO，重）—— 并发 + 信号量限流 + 同域礼貌间隔
  3. 抽取（LLM 调用，最慢）—— 丢线程池并发，否则 200 篇要跑半小时
  4. 入库（SQLite 写）—— 必须串行。Session 非线程安全，SQLite 也只有一个写入者

先全量发现再统一抓取，好处是能跨源去重：同一条新闻被两个站转载时只收一份。
"""
from __future__ import annotations

import asyncio
import itertools
import os
import urllib.parse
from datetime import datetime

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.knowledge import crawler, semantics
from app.knowledge.extract import extract
from app.knowledge.scoring import is_index_like, is_news_noise
from app.knowledge.sources import TOPIC_PROTOTYPES
from app.knowledge.store import MIN_CONTENT_CHARS, _clean_md, save_article
from app.models.knowledge import Article, CrawlRun, Source

FETCH_CONCURRENCY = 4
EXTRACT_CONCURRENCY = 4
DEFAULT_MAX_PER_SOURCE = 12

# 语义入库否决开关（默认关，原因见 _semantic_veto 的说明）
_VETO_ENABLED = os.environ.get("MODEL_VETO_ENABLED", "0") not in ("0", "false", "False")


def norm_domain(url_or_domain: str) -> str:
    s = (url_or_domain or "").strip().lower()
    host = urllib.parse.urlsplit(s).netloc if "//" in s else s
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- 阶段 0：准备
def load_sources(db: Session, only_domains: list[str] | None = None) -> list[dict]:
    q = db.query(Source).filter(Source.enabled.is_(True))
    rows = q.all()
    if only_domains:
        wanted = {norm_domain(d) for d in only_domains}
        rows = [s for s in rows if norm_domain(s.domain) in wanted]
    return [
        {"id": s.id, "name": s.name, "domain": norm_domain(s.domain),
         "list_urls": list(s.list_urls or []), "lang": s.lang,
         "category": s.category, "authority": s.authority}
        for s in rows
    ]


# ---------------------------------------------------------------- 阶段 1：发现
async def discover_all(backend, sources: list[dict],
                       max_per_source: int = DEFAULT_MAX_PER_SOURCE) -> tuple[list[dict], list[str]]:
    """并发发现候选文章链接，跨源按 URL 去重。"""
    errors: list[str] = []
    tasks = []
    for src in sources:
        for list_url in src["list_urls"]:
            tasks.append((src, list_url))

    async def one(src: dict, list_url: str) -> list[str]:
        try:
            return await crawler.discover(backend, list_url, src["domain"],
                                          max_links=max_per_source * 3)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src['name']} 发现失败: {type(e).__name__}: {e}")
            return []

    batches = await asyncio.gather(*(one(s, u) for s, u in tasks))

    # 一个源的多个栏目页**共享** max_per_source 名额，所以必须在栏目页之间
    # 轮转着取，不能按列表顺序取满。按顺序取的话第一个栏目页（实测
    # /physical-health 有 40 条）会把名额吃光，后面加的栏目页永远轮不到 ——
    # 新栏目会一直饿死，而且不报错，只会表现为「这个栏目一篇都没进来」。
    by_src: dict[int, list[list[str]]] = {}
    src_by_id: dict[int, dict] = {}
    for (src, _list_url), urls in zip(tasks, batches):
        by_src.setdefault(src["id"], []).append(urls)
        src_by_id[src["id"]] = src

    seen: set[str] = set()
    out: list[dict] = []
    for sid, groups in by_src.items():
        src = src_by_id[sid]
        taken = 0
        for i in itertools.count():          # 第 i 轮：从每个栏目页各取第 i 条
            if taken >= max_per_source:
                break
            progressed = False
            for urls in groups:
                if taken >= max_per_source:
                    break
                if i >= len(urls):
                    continue
                progressed = True
                u = urls[i]
                if u in seen:                # 跨源/跨栏目重复的不占名额
                    continue
                seen.add(u)
                taken += 1
                out.append({
                    "url": u, "domain": norm_domain(u),
                    "source_id": src["id"], "source_name": src["name"],
                    "lang": src["lang"], "authority": src["authority"],
                })
            if not progressed:               # 所有栏目页都取完了
                break
    return out, errors


# ---------------------------------------------------------------- 阶段 2：抓取
async def fetch_batch(backend, candidates: list[dict]) -> list[dict]:
    """并发抓正文。返回 [{candidate..., ok, title, text, published_at, error}]。

    后端是**整轮开始前挑一次**的（`crawler.open_backend`）：crawl4ai 装得上就
    整轮用它，单个 URL 导航失败时只返回 error，没有第二次机会。实测 ACSM 一轮
    8 篇里 4 篇 playwright 导航失败（`Failed on navigating ACS-GOTO`），直接丢了
    —— 每周任务是无人值守的，跑空一轮等于白等一周。所以这里对**失败的 URL**
    用 httpx 再补一次：JS 渲染站可能抓不到正文，但总比整篇丢掉强。
    """
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    fb_lock = asyncio.Lock()
    fb: list = []                      # 惰性进入上下文：SimpleBackend 要建立 httpx client

    async def _fallback():
        async with fb_lock:
            if not fb:
                b = crawler.SimpleBackend()
                await b.__aenter__()
                fb.append(b)
            return fb[0]

    async def one(c: dict) -> dict:
        async with sem:
            try:
                r = await backend.fetch(c["url"])
            except Exception as e:  # noqa: BLE001
                r = crawler.Fetched(url=c["url"], error=f"{type(e).__name__}: {e}")
            via = "primary"
            if not r.ok and isinstance(backend, crawler.Crawl4AIBackend):
                try:
                    r2 = await (await _fallback()).fetch(c["url"])
                    if r2.ok:
                        r, via = r2, "httpx-fallback"
                except Exception:  # noqa: BLE001  降级本身再失败就放弃
                    pass
        return {
            **c,
            "ok": r.ok,
            "title": r.title or r.html_title or "",
            "text": r.text,
            "published_at": r.published_at,
            "error": r.error,
            "fetch_via": via,
        }

    try:
        return list(await asyncio.gather(*(one(c) for c in candidates)))
    finally:
        if fb:
            await fb[0].__aexit__(None, None, None)


# ---------------------------------------------------------------- 阶段 3：抽取
async def extract_batch(fetched: list[dict]) -> list[dict]:
    """并发跑 LLM 抽取。同步的 openai 客户端丢线程池，否则阻塞事件循环。"""
    sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)

    async def one(f: dict) -> dict:
        async with sem:
            try:
                return await asyncio.to_thread(
                    extract, f["title"], f["text"], f.get("published_at"), f["domain"]
                )
            except Exception as e:  # noqa: BLE001
                return {
                    "title": f.get("title") or "", "organization": "", "author": "",
                    "published_at": None, "category": "training", "topics": [],
                    "summary": "", "key_points": [], "evidence_level": "unknown",
                    "extract_note": f"异常: {type(e).__name__}",
                }

    return list(await asyncio.gather(*(one(f) for f in fetched)))


# ---------------------------------------------------------------- 阶段 4：入库
def store_batch(db: Session, fetched: list[dict], extracted: list[dict]) -> dict:
    """串行入库。返回计数、按源明细与错误。

    counts 的 key 就是 save_article 返回的 action（inserted / updated / skipped /
    too_short / irrelevant / index_page / not_article / failed）。
    不做硬编码枚举 —— 以后加新闸门时不用回来改这里的汇总逻辑。
    """
    counts: dict = {}
    by_source: dict[str, dict] = {}
    errors: list[str] = []
    touched_sources: set[int] = set()
    kept = ("inserted", "updated")
    new_articles: list[Article] = []

    # 语义否决：用本地重排模型判定「这篇到底是不是我们要收的内容」。
    # 关键词闸门挡不住标题里带「医学」「健康」的行政文件，这一步能挡。
    # 服务不可用时返回空集合，整段逻辑自然跳过。
    veto = _semantic_veto(fetched, extracted)

    for idx, (f, fields) in enumerate(zip(fetched, extracted)):
        sname = f.get("source_name") or f["domain"]
        srow = by_source.setdefault(sname, {"source": sname, "domain": f["domain"],
                                           "fetched": 0, "inserted": 0, "rejected": 0})
        srow["fetched"] += 1
        if idx in veto:
            counts["not_relevant_ml"] = counts.get("not_relevant_ml", 0) + 1
            srow["rejected"] += 1
            continue
        try:
            action, art = save_article(
                db, url=f["url"], domain=f["domain"], raw_text=f["text"],
                title=fields.get("title") or f["title"],
                fallback_date=f.get("published_at"),
                source_id=f.get("source_id"), lang=f.get("lang", "zh"),
                source_authority=f.get("authority"), extracted=fields,
            )
        except Exception as e:  # noqa: BLE001
            db.rollback()
            counts["failed"] = counts.get("failed", 0) + 1
            srow["rejected"] += 1
            if len(errors) < 5:
                errors.append(f"入库失败 {f['url'][:70]}: {type(e).__name__}: {e}")
            continue

        counts[action] = counts.get(action, 0) + 1
        if action in kept:
            srow["inserted"] += 1
            if art is not None:
                new_articles.append(art)
            if f.get("source_id"):
                touched_sources.add(f["source_id"])
        else:
            srow["rejected"] += 1
        db.commit()

    # 向量：入库后统一批量算，一篇一次 HTTP 太浪费
    embedded = _attach_embeddings(db, new_articles)

    if touched_sources:
        (db.query(Source)
           .filter(Source.id.in_(touched_sources))
           .update({Source.last_crawled_at: datetime.now()}, synchronize_session=False))
        db.commit()

    skipped = sum(v for k, v in counts.items() if k not in kept and k != "failed")

    return {"counts": counts, "skipped": skipped, "errors": errors,
            "embedded": embedded, "semantic": semantics.available(ttl=0),
            "by_source": sorted(by_source.values(), key=lambda x: -x["inserted"])}


# ---------------------------------------------------------------- 语义增强
def _semantic_veto(fetched: list[dict], extracted: list[dict]) -> set[int]:
    """用本地重排模型挑出「明显不是我们要的内容」的下标。

    ⚠️ **默认关闭**，需要显式设 MODEL_VETO_ENABLED=1 才生效。

    实测结论：重排分的绝对校准**依赖具体查询**，把它当固定阈值的入库门不可靠。
      · 用具体问题当查询时区分度很好：相关文档 +4.6/+3.9/-0.7，无关 -10.3；
      · 但换成宽泛的「主题原型」查询后分数整体被压低、区分度消失 ——
        在真实语料上按 -6 阈值会砍掉 48/75 篇，连《体育专家解读22个健身误区》
        《Sleep and Immunity》《How to stretch after exercising》都在里面。
      · 试过改用「文章向量 vs 原型向量」的最大余弦，同样不校准
        （"FDA approves a flu vaccine" 排第一、"运动减肥误区" 排倒数第二）。

    所以语义能力只用在该用的地方：**检索时对具体问题做重排**。
    入库闸门继续交给确定性的规则（正文长度 / 目录页 / 机构新闻 / 关键词相关度），
    那些规则可解释、可回归。这段代码留着是为了将来给某个明确的垂直话题
    定制一组原型时能直接用。
    """
    if not _VETO_ENABLED or not semantics.available():
        return set()

    idx: list[int] = []
    docs: list[str] = []
    for i, f in enumerate(fetched):
        content = _clean_md(f.get("text") or "")
        if len(content) < MIN_CONTENT_CHARS or is_index_like(content):
            continue
        fields = extracted[i] if i < len(extracted) else {}
        title = fields.get("title") or f.get("title") or ""
        if is_news_noise(title):
            continue
        idx.append(i)
        docs.append(semantics.doc_text(
            title, fields.get("summary", ""), fields.get("key_points"),
            content, limit=1500))

    if not docs:
        return set()

    best = semantics.topic_scores(docs, TOPIC_PROTOTYPES)
    if best is None:
        return set()
    return {idx[j] for j, s in enumerate(best) if s < semantics.RERANK_VETO}


def _attach_embeddings(db: Session, articles: list[Article]) -> int:
    """给刚入库的文章补向量。返回成功条数。

    只算「标题 + 摘要 + 要点」，和重排用的是同一份文本。
    存的是 JSON 数组，1024 维；embed_model 记下模型标识，便于将来换模型时重算。
    """
    if not articles or not semantics.available():
        return 0
    targets = [a for a in articles if not a.embedding]
    if not targets:
        return 0

    texts = [semantics.doc_text(a.title, a.summary, a.key_points, a.content_md)
             for a in targets]
    vecs = semantics.embed(texts)
    if not vecs or len(vecs) != len(targets):
        return 0
    for a, v in zip(targets, vecs):
        a.embedding = semantics.pack(v)
        a.embed_model = "qwen3-embedding-0.6b"
    db.commit()
    return len(targets)


# ---------------------------------------------------------------- 质量校验
def verify_round(fetched: list[dict], extracted: list[dict], counts: dict,
                 by_source: list[dict] | None = None) -> dict:
    """一轮爬取后的自检。

    看的是「这轮结果可不可信」，不是「有没有出错」。
    常见的假成功：HTTP 200 但内容是验证码页、列表页改版导致 0 发现、
    LLM key 失效导致摘要全空。这些都不会抛异常，只能靠校验抓出来。

    ⚠️ 必须按源看，不能只看总数。
    实测踩过：全量轮次总入库 17 篇（Harvard/NHS/ACSM 贡献），
    于是总体判定 quality=ok —— 而同时有 10 个源 fetched=10 / inserted=0。
    总数掩盖了半数源已经静默失效的事实。
    """
    flags: list[str] = []   # 真问题，会拉低质量评级
    notes: list[str] = []   # 仅供观察的信息，不影响评级
    ok_fetched = [f for f in fetched if f.get("ok") and len(f.get("text") or "") >= 300]

    if not fetched:
        flags.append("没有抓到任何页面：所有列表页可能都失败了")
    else:
        fail_rate = 1 - len(ok_fetched) / len(fetched)
        if fail_rate > 0.5:
            flags.append(f"抓取失败率 {fail_rate:.0%}，可能有反爬或网络问题")

    if ok_fetched and not any(f.get("summary") for f in extracted):
        flags.append("抽取结果全为空：LLM key 或模型调用有问题")

    if ok_fetched and counts.get("inserted", 0) == 0 and counts.get("updated", 0) == 0:
        flags.append("抓到内容但一条都没入库：检查主题闸门与正文长度阈值")

    if counts.get("irrelevant", 0) > max(3, len(ok_fetched) * 0.5):
        flags.append("过半内容被判跑题：列表页可能抓成了导航页或分类页")

    # 按源自检：抓到 ≥3 篇却一篇没入库的源，必然是列表页选错或闸门误杀
    if by_source:
        dead = [s["source"] for s in by_source
                if s.get("fetched", 0) >= 3 and s.get("inserted", 0) == 0]
        if dead:
            flags.append(f"{len(dead)} 个源抓到≥3篇但0入库，需排查列表页："
                         + "、".join(dead[:6]))
    # 拒绝原因分布，直接指出是哪道闸门在杀（信息，不算问题）
    reasons = {k: v for k, v in counts.items()
               if k not in ("inserted", "updated") and v}
    if reasons:
        top = sorted(reasons.items(), key=lambda x: -x[1])[:5]
        notes.append("无效内容构成：" + "，".join(f"{k} {v}" for k, v in top))
    # crawl4ai 单 URL 失败后走 httpx 补抓的篇数。数字突然变大说明浏览器后端
    # 出问题了（驱动没了、被反爬挡了），值得单独看一眼。
    rescued = sum(1 for f in fetched if f.get("fetch_via") == "httpx-fallback")
    if rescued:
        notes.append(f"{rescued} 篇 crawl4ai 抓取失败、由 httpx 补抓成功"
                     f"（浏览器后端可能在退化，留意后续轮次）")

    quality = "ok" if not flags else ("warn" if len(flags) == 1 else "bad")
    return {"quality": quality, "flags": flags, "notes": notes,
            "ok_fetched": len(ok_fetched), "candidates": len(fetched)}


# ---------------------------------------------------------------- 记录落库
def open_run(db: Session, trigger: str) -> int:
    run = CrawlRun(trigger=trigger, status="running", log=[])
    db.add(run)
    db.commit()
    db.refresh(run)
    return run.id


def finish_run(db: Session, run_id: int, *, status: str, totals: dict,
               log: list, error: str = "") -> None:
    run = db.get(CrawlRun, run_id)
    if not run:
        return
    run.status = status
    run.finished_at = datetime.now()
    run.discovered = totals.get("discovered", 0)
    run.fetched = totals.get("fetched", 0)
    run.inserted = totals.get("inserted", 0)
    run.updated = totals.get("updated", 0)
    run.skipped = totals.get("skipped", 0)
    run.failed = totals.get("failed", 0)
    run.log = log
    run.error = error[:4000] if error else ""
    db.commit()


# ---------------------------------------------------------------- 一体化入口（脚本用）
async def crawl_once(trigger: str = "weekly", max_per_source: int = DEFAULT_MAX_PER_SOURCE,
                     only_domains: list[str] | None = None,
                     prefer: str = "crawl4ai") -> dict:
    """不开图，直接顺序跑一遍全部阶段。给命令行脚本和调试用。"""
    db = SessionLocal()
    run_id = open_run(db, trigger)
    try:
        sources = load_sources(db, only_domains)
        if not sources:
            raise RuntimeError("没有启用的数据源")

        backend, backend_note = await crawler.open_backend(prefer)
        async with backend:
            candidates, derr = await discover_all(backend, sources, max_per_source)
            print(f"发现候选 {len(candidates)} 条，开始抓取…")
            fetched = await fetch_batch(backend, candidates)
            good = [f for f in fetched if f["ok"] and len(f["text"]) >= 300]
            print(f"抓取成功 {len(good)} / {len(fetched)}，开始 LLM 抽取…")
            extracted = await extract_batch(good)

        stored = store_batch(db, good, extracted)
        check = verify_round(fetched, extracted, stored["counts"], stored["by_source"])
        counts = stored["counts"]

        totals = {
            "discovered": len(candidates),
            "fetched": len(good),
            "inserted": counts.get("inserted", 0),
            "updated": counts.get("updated", 0),
            "skipped": stored["skipped"],
            "failed": counts.get("failed", 0) + (len(fetched) - len(good)),
        }
        log = [{"per_source": stored["by_source"],
                "errors": derr + stored["errors"], "check": check}]
        finish_run(db, run_id, status="done", totals=totals, log=log)

        return {"run_id": run_id, "status": "done", "trigger": trigger,
                "backend": backend_note, **totals,
                "quality": check["quality"], "flags": check["flags"],
                "notes": check.get("notes", []),
                "by_source": stored["by_source"], "errors": derr + stored["errors"]}
    except Exception as e:  # noqa: BLE001
        import traceback

        finish_run(db, run_id, status="failed", totals={}, log=[],
                   error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
    finally:
        db.close()
