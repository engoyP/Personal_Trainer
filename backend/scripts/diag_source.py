"""单源诊断：逐条打印候选文章走完闸门后的失败原因。

排查「某源 fetched=10 却 inserted=0」这类静默失效时用。
它不改库、不写库，只报告每一步的判定，方便判断是
「发现的根本不是文章」还是「闸门把好文章误杀了」。

    ./.venv/Scripts/python.exe scripts/diag_source.py --domain sport.gov.cn
    ./.venv/Scripts/python.exe scripts/diag_source.py --domain cdc.gov --limit 5
    ./.venv/Scripts/python.exe scripts/diag_source.py --domain dxy.com --no-llm
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def diag(domain: str, limit: int, use_llm: bool, backend_name: str) -> int:
    from app.database import SessionLocal, init_db
    from app.knowledge import crawler
    from app.knowledge.pipeline import load_sources, norm_domain
    from app.knowledge.scoring import classify, is_index_like, link_ratio
    from app.knowledge.store import MIN_CONTENT_CHARS, _clean_md

    init_db()
    db = SessionLocal()
    try:
        sources = load_sources(db, [domain])
    finally:
        db.close()

    if not sources:
        print(f"没有匹配的源：{domain}")
        return 1

    src = sources[0]
    print(f"源：{src['name']}  域：{src['domain']}  authority={src['authority']}")
    print(f"列表页：{src['list_urls']}\n")

    backend, note = await crawler.open_backend(backend_name)
    print(f"后端：{note}\n")

    async with backend:
        # ---- 发现
        for list_url in src["list_urls"]:
            found = await crawler.discover(backend, list_url, src["domain"], max_links=limit)
            print(f"[发现] {list_url}")
            print(f"       → {len(found)} 条候选文章链接")
            for u in found:
                print(f"         · {u}")
            print()

        all_found: list[str] = []
        for list_url in src["list_urls"]:
            all_found += await crawler.discover(backend, list_url, src["domain"],
                                                max_links=limit)
        # 去重保序
        seen: set[str] = set()
        cands = [u for u in all_found if not (u in seen or seen.add(u))][:limit]

        if not cands:
            print("⚠️  没发现任何候选文章链接 —— 列表页结构变了或列表页选的栏目不对")
            return 0

        print("=" * 100)
        print(f"逐条诊断前 {len(cands)} 条\n")

        for i, url in enumerate(cands, 1):
            r = await backend.fetch(url)
            if not r.ok:
                print(f"{i:>2}. ✗ 抓取失败  {url}")
                print(f"      {r.error}")
                continue

            content = _clean_md(crawler.strip_leading_nav(r.markdown))
            n = len(content)
            ratio = link_ratio(content)
            idx = is_index_like(content)

            reason = ""
            if n < MIN_CONTENT_CHARS:
                reason = f"too_short (<{MIN_CONTENT_CHARS})"
            elif idx:
                reason = f"index_page (link_ratio={ratio})"
            else:
                cat, topics, rel = classify(f"{r.title}\n{content}")
                if rel < 0.25:
                    reason = f"irrelevant (relevance={rel}, topics={topics})"
                else:
                    reason = f"PASS闸门 (relevance={rel}, cat={cat}, topics={topics})"

            mark = "✓" if reason.startswith("PASS") else "✗"
            print(f"{i:>2}. {mark} {r.title[:60] or '(无标题)'}")
            print(f"      {url}")
            print(f"      正文字符={n}  链接占比={ratio}  判定={reason}")
            if use_llm and not reason.startswith("too_short"):
                from app.knowledge.extract import extract
                try:
                    fields = extract(r.title, content, r.published_at, src["domain"])
                    print(f"      LLM: substantive={fields.get('substantive')} "
                          f"cat={fields.get('category')} ev={fields.get('evidence_level')}")
                    print(f"      摘要: {(fields.get('summary') or '')[:100]}")
                except Exception as e:  # noqa: BLE001
                    print(f"      LLM 异常: {type(e).__name__}: {e}")
            else:
                snippet = content[:160].replace("\n", " ")
                print(f"      正文开头: {textwrap.shorten(snippet, 150)}")
            print()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="单源入库诊断")
    ap.add_argument("--domain", required=True, help="要诊断的域名")
    ap.add_argument("--limit", type=int, default=10, help="最多检查多少条候选")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 抽取（只看闸门）")
    ap.add_argument("--backend", default="crawl4ai", choices=["crawl4ai", "httpx"])
    args = ap.parse_args()
    return asyncio.run(diag(args.domain, args.limit, not args.no_llm, args.backend))


if __name__ == "__main__":
    raise SystemExit(main())
