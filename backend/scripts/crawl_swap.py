"""爬取「食材替换 / 食物替代」相关知识，供教练回答"我没有XX能换什么"。

用法（在 backend 目录）：
    .venv/Scripts/python.exe scripts/crawl_swap.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.graph import run_knowledge

KEYWORDS = [
    "食材替换 减脂餐 替代",
    "食物替代 同等营养 热量",
    "减脂 蔬菜替代 蛋白质来源替换",
    "橄榄油 替代 食用油 健康",
    "灵活饮食 食材互换 减脂",
]


def main() -> int:
    total = 0
    for i, kw in enumerate(KEYWORDS, 1):
        print(f"\n[{i}/{len(KEYWORDS)}] {kw}", flush=True)
        try:
            out = run_knowledge(trigger="manual", keyword=kw, max_per_source=6)
            ins = (out.get("counts") or {}).get("inserted", 0)
            total += ins
            print(f"  状态={out.get('status')} 入库={ins}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  失败：{type(e).__name__}: {e}", flush=True)
        time.sleep(3)
    print(f"\n=== 完成，累计入库 {total} 篇 ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
