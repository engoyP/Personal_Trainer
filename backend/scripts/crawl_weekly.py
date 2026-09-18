"""每周爬取入口：跑一遍知识子图，把权威运动训练健康文章结构化入库。

定时任务直接调这个脚本：
    ./.venv/Scripts/python.exe scripts/crawl_weekly.py

常用参数：
    --max-per-source 8      每个源最多收几篇（默认 12）
    --domains sport.gov.cn,acsm.org   只跑指定域名（调试用，跑得快）
    --backend httpx         强制用 httpx 降级后端（crawl4ai 出问题时）
    --trigger weekly        标记这轮的来源
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description="每周爬取训练知识库")
    ap.add_argument("--max-per-source", type=int, default=12)
    ap.add_argument("--domains", default="", help="逗号分隔，只跑这些域名")
    ap.add_argument("--backend", default="crawl4ai", choices=["crawl4ai", "httpx"])
    ap.add_argument("--trigger", default="weekly")
    args = ap.parse_args()

    from app.database import init_db
    from app.knowledge.graph import run_knowledge

    init_db()  # 建表 + 灌源清单

    only = [d.strip() for d in args.domains.split(",") if d.strip()] or None

    print("=" * 64)
    print(f"知识库爬取开始  trigger={args.trigger}  每源上限={args.max_per_source}")
    print(f"后端={args.backend}  域名过滤={only or '全部'}")
    print("=" * 64)

    t0 = time.time()
    result = run_knowledge(
        trigger=args.trigger,
        max_per_source=args.max_per_source,
        prefer_backend=args.backend,
        only_domains=only,
    )
    elapsed = time.time() - t0

    print()
    print("-" * 64)
    print(result.get("report") or "（无报告）")
    print("-" * 64)
    print(f"耗时 {elapsed:.0f} 秒  run_id={result.get('run_id')}")

    by = result.get("by_source") or []
    if by:
        print()
        print("按源明细：")
        for s in by:
            print(f"  {s['source'][:24]:<26} 抓到 {s['fetched']:>3}  新增 {s['inserted']:>3}"
                  f"  判为无效 {s['rejected']:>3}")

    # 无效内容的构成，用来判断闸门是不是卡太紧或太松
    counts = result.get("counts") or {}
    rejected = {k: v for k, v in counts.items()
                if k not in ("inserted", "updated", "failed") and v}
    if rejected:
        labels = {
            "index_page": "目录/索引页", "not_article": "模型判定非文章",
            "irrelevant": "主题不相关", "too_short": "正文过短",
            "news_item": "机构新闻/行政事务",
        }
        print()
        print("无效内容构成：" + "，".join(
            f"{labels.get(k, k)} {v}" for k, v in sorted(rejected.items(), key=lambda x: -x[1])))

    errs = result.get("errors") or []
    if errs:
        print()
        print("错误：")
        for e in errs[:8]:
            print(f"  - {e[:150]}")

    # 质量 bad 时返回非 0，定时任务层面能感知到本轮不可信
    if (result.get("check") or {}).get("quality") == "bad":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
