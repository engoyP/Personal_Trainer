"""验证膳食指南官网的栏目页：能发现多少文章 + 正文质量。

    ./.venv/Scripts/python.exe scripts/probe_dg.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DOMAIN = "dg.cnsoc.org"
COLS = [
    "https://dg.cnsoc.org/newslist_0402_1.htm",       # 膳食指南（2022）平衡膳食
    "https://dg.cnsoc.org/gzdtnewslist_0406_2_1.htm",  # 指南解读
    "https://dg.cnsoc.org/imgnewslist_0602_1.htm",     # 图示和工具
]


async def main() -> int:
    from app.knowledge import crawler

    backend, _ = await crawler.open_backend("crawl4ai")
    async with backend:
        all_links: list[str] = []
        for col in COLS:
            links = await crawler.discover(backend, col, DOMAIN, max_links=12)
            print(f"栏目 {col.split('/')[-1]:34s} 发现 {len(links)} 篇")
            all_links += links
        print()
        # 抓前 3 篇看正文
        for u in all_links[:3]:
            f = await backend.fetch(u)
            txt = f.text or ""
            print(f"── {u[-48:]}")
            print(f"   ok={f.ok} 正文={len(txt)} 字 | 标题: {(f.title or '')[:40]}")
            if txt:
                print(f"   开头: {txt[:150].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
