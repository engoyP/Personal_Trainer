"""找候选源的科普栏目页：抓首页，列出站内栏目链接。

    ./.venv/Scripts/python.exe scripts/probe_cols.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SITES = [
    ("膳食指南", "dg.cnsoc.org", "https://dg.cnsoc.org/"),
    ("上海疾控", "scdc.sh.cn", "https://www.scdc.sh.cn/"),
    ("北京疾控", "bjcdc.org", "https://www.bjcdc.org/"),
]


async def main() -> int:
    from app.knowledge import crawler
    from app.knowledge.crawler import normalize_url, same_site

    backend, _ = await crawler.open_backend("crawl4ai")
    async with backend:
        for name, domain, url in SITES:
            f = await backend.fetch(url)
            if not f.ok:
                print(f"── {name}: 抓取失败 {f.error[:40]}")
                continue
            links = re.findall(r"\[([^\]]{2,24})\]\((https?://[^\)]+)\)", f.markdown or "")
            seen: dict[str, str] = {}
            for t, u in links:
                if same_site(u, domain):
                    seen.setdefault(normalize_url(u, url), t)
            print(f"\n── {name} ({domain}) 站内链接 {len(seen)} 个")
            for u, t in list(seen.items())[:16]:
                print(f"   {t[:18]:20s} {u[:78]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
