"""知识子图：discover → crawl → extract → store → verify → report

这是一个独立可跑的 LangGraph 图，也可以被别的主图当子图挂上去
（`g.add_node("knowledge", get_knowledge_graph())`）。

刻意**不挂 checkpointer**：每周任务没有人机交互，不需要断点续跑；
不挂的好处是 state 里可以放 dataclass、Fetched 这类原生对象，
不用为了 msgpack 序列化把它们全拆成 dict。观测需求交给 kb_crawl_runs 表。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.database import SessionLocal
from app.knowledge import crawler, pipeline
from app.knowledge.pipeline import DEFAULT_MAX_PER_SOURCE
from app.services import trace


class KnowledgeState(TypedDict, total=False):
    # 输入
    trigger: str                      # manual / weekly
    keyword: str                      # 关键词搜全网（非空则走搜索路径，不依赖源清单）
    image_ocr: bool                   # 是否对正文图片做 OCR 补文字
    min_authority: float              # 关键词搜索的域名权威门槛（0=不过滤）
    max_per_source: int
    prefer_backend: str               # crawl4ai / httpx
    only_domains: list[str] | None

    # 中间产物
    run_id: int
    backend_note: str
    sources: list[dict]
    candidates: list[dict]
    fetched: list[dict]
    extracted: list[dict]
    skipped: int

    # 输出
    counts: dict
    check: dict
    by_source: list[dict]
    errors: list[str]
    report: str
    status: str


# ---------------------------------------------------------------- 节点
def n_prepare(state: KnowledgeState) -> dict:
    """挑出启用的数据源，开一条 CrawlRun 记录。关键词模式不依赖源清单。"""
    db = SessionLocal()
    try:
        sources = pipeline.load_sources(db, state.get("only_domains"))
        keyword = (state.get("keyword") or "").strip()
        # 关键词模式：源清单为空也能跑（走搜索发现），普通模式才要求有源
        if not sources and not keyword:
            return {"status": "failed", "errors": ["没有启用的数据源"],
                    "report": "本轮未执行：数据源表为空或全部被禁用。"}
        run_id = pipeline.open_run(db, state.get("trigger", "manual"))
        return {"sources": sources, "run_id": run_id, "errors": []}
    finally:
        db.close()


async def n_discover(state: KnowledgeState) -> dict:
    """发现候选链接：有关键词走全网搜索，否则从源站栏目页爬。"""
    if state.get("status") == "failed":
        return {}
    errors = list(state.get("errors") or [])
    keyword = (state.get("keyword") or "").strip()

    if keyword:
        from app.knowledge.search import search_web

        max_n = state.get("max_per_source") or DEFAULT_MAX_PER_SOURCE
        urls, note = search_web(keyword, count=max_n,
                                min_authority=state.get("min_authority") or 0.0)
        candidates = [
            {"url": u, "domain": pipeline.norm_domain(u), "source_id": 0,
             "source_name": f"搜索·{keyword}", "lang": "zh", "authority": 0.5}
            for u in urls
        ]
        if not candidates:
            errors.append(f"关键词「{keyword}」搜索无结果：{note}")
        return {"candidates": candidates, "backend_note": note, "errors": errors}

    max_n = state.get("max_per_source") or DEFAULT_MAX_PER_SOURCE
    backend, note = await crawler.open_backend(state.get("prefer_backend", "crawl4ai"))
    async with backend:
        candidates, errors2 = await pipeline.discover_all(backend, state["sources"], max_n)
    return {"candidates": candidates, "backend_note": note,
            "errors": errors + errors2}


async def n_crawl(state: KnowledgeState) -> dict:
    """并发抓正文，再对正文里的图片做 OCR 补文字。"""
    if state.get("status") == "failed" or not state.get("candidates"):
        return {"fetched": []}
    backend, _ = await crawler.open_backend(state.get("prefer_backend", "crawl4ai"))
    async with backend:
        fetched = await pipeline.fetch_batch(backend, state["candidates"])

    # 图片 OCR：图解类内容的文字在图片里，纯文本抽取会漏。服务不可用静默跳过。
    errors = list(state.get("errors") or [])
    if state.get("image_ocr", True):
        try:
            from app.knowledge import image_ocr

            n = image_ocr.enrich(fetched)
            if n:
                trace.emit("tool", "图片 OCR", f"补了 {n} 张图的文字")
        except Exception as e:  # noqa: BLE001
            errors.append(f"图片 OCR 失败（已跳过）：{type(e).__name__}")
    return {"fetched": fetched, "errors": errors}


async def n_extract(state: KnowledgeState) -> dict:
    """并发 LLM 抽取。只喂抓成功且够长的。"""
    good = [f for f in (state.get("fetched") or [])
            if f.get("ok") and len(f.get("text") or "") >= 300]
    if not good:
        return {"extracted": []}
    return {"extracted": await pipeline.extract_batch(good)}


def n_store(state: KnowledgeState) -> dict:
    """串行入库。"""
    good = [f for f in (state.get("fetched") or [])
            if f.get("ok") and len(f.get("text") or "") >= 300]
    if not good:
        return {"counts": {}, "by_source": [], "skipped": 0}

    db = SessionLocal()
    try:
        result = pipeline.store_batch(db, good, state.get("extracted") or [])
        return {"counts": result["counts"], "by_source": result["by_source"],
                "skipped": result["skipped"],
                "errors": list(state.get("errors") or []) + result["errors"]}
    finally:
        db.close()


def n_verify(state: KnowledgeState) -> dict:
    """本轮自检。假成功比报错更危险，所以这一步不能省。"""
    check = pipeline.verify_round(state.get("fetched") or [],
                                  state.get("extracted") or [],
                                  state.get("counts") or {},
                                  state.get("by_source") or [])
    return {"check": check}


def n_report(state: KnowledgeState) -> dict:
    """写回 CrawlRun 并生成一句人能看懂的结论。"""
    fetched = state.get("fetched") or []
    counts = state.get("counts") or {}
    check = state.get("check") or {}
    good = [f for f in fetched if f.get("ok") and len(f.get("text") or "") >= 300]

    totals = {
        "discovered": len(state.get("candidates") or []),
        "fetched": len(good),
        "inserted": counts.get("inserted", 0),
        "updated": counts.get("updated", 0),
        "skipped": state.get("skipped") or 0,
        "failed": counts.get("failed", 0) + (len(fetched) - len(good)),
    }

    errors = state.get("errors") or []
    report = _render(totals, check, state.get("by_source") or [],
                     state.get("backend_note", ""), errors)

    db = SessionLocal()
    try:
        pipeline.finish_run(
            db, state["run_id"],
            status="done" if check.get("quality") != "bad" else "warn",
            totals=totals,
            log=[{"per_source": state.get("by_source") or [],
                  "errors": errors[:20], "check": check}],
            error="\n".join(errors[:10]),
        )
    finally:
        db.close()

    return {"report": report, "status": "done"}


def _render(totals: dict, check: dict, by_source: list[dict],
            backend_note: str, errors: list[str]) -> str:
    lines = [
        f"本轮爬取：新增 {totals['inserted']} 篇，更新 {totals['updated']} 篇，"
        f"跳过 {totals['skipped']} 篇，失败 {totals['failed']} 篇"
        f"（发现 {totals['discovered']}，抓成功 {totals['fetched']}）",
        f"抓取后端：{backend_note or '未知'}",
        f"质量判定：{check.get('quality', 'unknown')}",
    ]
    if check.get("flags"):
        lines.append("需要注意：" + "；".join(check["flags"]))
    if check.get("notes"):
        lines.append("观察：" + "；".join(check["notes"]))
    if by_source:
        top = "，".join(f"{s['source']} {s['inserted']}" for s in by_source[:6] if s["inserted"])
        if top:
            lines.append("入库来源 Top：" + top)
    if errors:
        lines.append("错误摘录：" + "；".join(errors[:3]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 组图
def build_knowledge_graph():
    g = StateGraph(KnowledgeState)
    t = trace.node  # 节点埋点，让爬取过程能在执行链路面板看到
    g.add_node("prepare", t("准备源清单")(n_prepare))
    g.add_node("discover", t("发现候选链接")(n_discover))
    g.add_node("crawl", t("抓正文 + 图片OCR")(n_crawl))
    g.add_node("extract", t("LLM 抽取摘要要点")(n_extract))
    g.add_node("store", t("校验入库")(n_store))
    g.add_node("verify", t("假成功自检")(n_verify))
    g.add_node("report", t("生成报告")(n_report))

    g.set_entry_point("prepare")
    g.add_edge("prepare", "discover")
    g.add_edge("discover", "crawl")
    g.add_edge("crawl", "extract")
    g.add_edge("extract", "store")
    g.add_edge("store", "verify")
    g.add_edge("verify", "report")
    g.add_edge("report", END)

    # 无 checkpointer：每周批量任务不需要断点续跑
    return g.compile()


_graph = None


def get_knowledge_graph():
    global _graph
    if _graph is None:
        _graph = build_knowledge_graph()
    return _graph


async def run_knowledge_async(*, trigger: str = "weekly",
                             keyword: str = "",
                             image_ocr: bool = True,
                             min_authority: float = 0.0,
                             max_per_source: int = DEFAULT_MAX_PER_SOURCE,
                             prefer_backend: str = "crawl4ai",
                             only_domains: list[str] | None = None) -> dict:
    """跑一遍知识子图（异步）。

    discover / crawl / extract 三个节点是 async 的，必须走 ainvoke ——
    LangGraph 不会替你把协程跑起来，用同步 invoke 会直接报
    "No synchronous function provided"。
    """
    g = get_knowledge_graph()
    out = await g.ainvoke({
        "trigger": trigger,
        "keyword": keyword,
        "image_ocr": image_ocr,
        "min_authority": min_authority,
        "max_per_source": max_per_source,
        "prefer_backend": prefer_backend,
        "only_domains": only_domains,
        "errors": [],
    })
    return _shape(out)


def run_knowledge(*, trigger: str = "weekly",
                  keyword: str = "",
                  image_ocr: bool = True,
                  min_authority: float = 0.0,
                  max_per_source: int = DEFAULT_MAX_PER_SOURCE,
                  prefer_backend: str = "crawl4ai",
                  only_domains: list[str] | None = None) -> dict:
    """同步包装。给命令行脚本和 FastAPI 后台任务用。

    只在没有事件循环的线程里调用 —— 后台任务跑在线程池里，所以是安全的。
    如果在协程里调用，请直接用 run_knowledge_async。
    """
    import asyncio

    return asyncio.run(run_knowledge_async(
        trigger=trigger, keyword=keyword, image_ocr=image_ocr,
        min_authority=min_authority, max_per_source=max_per_source,
        prefer_backend=prefer_backend, only_domains=only_domains,
    ))


def _shape(out: dict) -> dict:
    return {
        "run_id": out.get("run_id"),
        "status": out.get("status"),
        "report": out.get("report", ""),
        "counts": out.get("counts") or {},
        "check": out.get("check") or {},
        "by_source": out.get("by_source") or [],
        "backend": out.get("backend_note", ""),
        "errors": out.get("errors") or [],
    }
