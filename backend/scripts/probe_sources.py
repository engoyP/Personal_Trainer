"""源健康度探针：逐个探测数据源的列表页，报告是否可达、能发现多少文章链接。

站点改版、加反爬、栏目页迁移都会让某个源静默失效（0 发现）。
这个脚本用来快速定位是哪个源坏了，改完 sources.py 再跑一次验证。

    ./.venv/Scripts/python.exe scripts/probe_sources.py
    ./.venv/Scripts/python.exe scripts/probe_sources.py --domains acsm.org,who.int
    ./.venv/Scripts/python.exe scripts/probe_sources.py --backend httpx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def probe(domains: list[str] | None, backend_name: str) -> int:
    from app.database import SessionLocal, init_db
    from app.knowledge import crawler

    init_db()
    db = SessionLocal()
    try:
        from app.knowledge.pipeline import load_sources

        sources = load_sources(db, domains)
    finally:
        db.close()

    if not sources:
        print("没有匹配的数据源")
        return 1

    backend, note = await crawler.open_backend(backend_name)
    print(f"后端：{note}")
    print(f"探测 {len(sources)} 个源\n")
    print(f"{'站点':<22}{'状态':<10}{'正文':>8}{'链接':>7}{'文章':>7}  首个列表页")
    print("-" * 96)

    bad: list[str] = []
    async with backend:
        for src in sources:
            total_art = 0
            total_links = 0
            ok_any = False
            body = 0
            first_url = ""
            note_txt = ""

            for list_url in (src["list_urls"] or [])[:2]:
                try:
                    r = await backend.fetch(list_url)
                except Exception as e:  # noqa: BLE001
                    note_txt = f"{type(e).__name__}"
                    continue
                if r.ok:
                    ok_any = True
                    total_links += len(r.links)
                    body = max(body, len(r.text))
                    first_url = first_url or list_url
                    arts = [u for u in r.links if crawler.is_article_like(u)
                            and crawler.same_site(u, src["domain"])]
                    total_art += len(arts)
                else:
                    note_txt = (r.error or "")[:40]

            status = "OK" if ok_any and total_art else ("可达/0文章" if ok_any else "不可达")
            if not (ok_any and total_art):
                bad.append(src["name"])

            print(f"{src['name'][:20]:<22}{status:<10}{body:>8}{total_links:>7}"
                  f"{total_art:>7}  {(first_url or note_txt)[:42]}")

    print()
    if bad:
        print(f"需要关注的源（{len(bad)} 个）：")
        for b in bad:
            print(f"  - {b}")
        print("\n处理方式：改 app/knowledge/sources.py 里对应的 list_urls，")
        print("或把该源的 enabled 置 False；被反爬硬拦（如 Akamai）的站点建议直接停用。")
    else:
        print("全部源正常。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="探测知识库数据源健康度")
    ap.add_argument("--domains", default="", help="逗号分隔，只探测这些域名")
    ap.add_argument("--backend", default="crawl4ai", choices=["crawl4ai", "httpx"])
    args = ap.parse_args()

    only = [d.strip() for d in args.domains.split(",") if d.strip()] or None
    t0 = time.time()
    code = asyncio.run(probe(only, args.backend))
    print(f"\n耗时 {time.time() - t0:.0f} 秒")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
