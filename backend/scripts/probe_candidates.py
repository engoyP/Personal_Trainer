"""候选权威源探测：确认哪些中文营养/健康科普源能抓、有文章栏目。

用途：给知识库找「定向权威源」时先实测，通过后再写进 sources.py。
只探测不写库。

    ./.venv/Scripts/python.exe scripts/probe_candidates.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (名称, 域名, 候选栏目页 URL)
CANDIDATES = [
    ("中国营养学会", "chinanutri.cn", [
        "https://www.chinanutri.cn/",
        "https://www.chinanutri.cn/kpjy/",
        "https://www.chinanutri.cn/yykp/",
    ]),
    ("膳食指南官网", "dg.cnsoc.org", [
        "https://dg.cnsoc.org/",
        "https://dg.cnsoc.org/articleList.html",
    ]),
    ("广东省疾控中心", "gdcdc.cn", [
        "https://www.gdcdc.cn/",
        "https://www.gdcdc.cn/kpjy/",
    ]),
    ("上海市疾控中心", "scdc.sh.cn", [
        "https://www.scdc.sh.cn/",
    ]),
    ("北京市疾控中心", "bjcdc.org", [
        "https://www.bjcdc.org/",
    ]),
    ("国家卫健委", "nhc.gov.cn", [
        "http://www.nhc.gov.cn/",
    ]),
    ("中国食品安全风险评估中心", "cfsa.net.cn", [
        "https://www.cfsa.net.cn/",
    ]),
    ("中国疾控营养与健康所", "ninh.chinacdc.cn", [
        "https://ninh.chinacdc.cn/",
    ]),
]


async def main() -> int:
    from app.knowledge import crawler

    backend, note = await crawler.open_backend("crawl4ai")
    print(f"backend: {note}\n")
    async with backend:
        for name, domain, urls in CANDIDATES:
            print(f"── {name} ({domain})")
            for u in urls:
                try:
                    links = await crawler.discover(backend, u, domain, max_links=12)
                    f = await backend.fetch(u)
                    title = (f.title or "")[:36] if f.ok else f"FAIL:{f.error[:24]}"
                    print(f"   {u[:56]:58s} links={len(links):<3d} {title}")
                except Exception as e:  # noqa: BLE001
                    print(f"   {u[:56]:58s} 异常 {type(e).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
