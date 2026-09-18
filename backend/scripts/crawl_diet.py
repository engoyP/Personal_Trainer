"""批量爬取「减脂饮食 / 食材热量 / 运动营养」相关关键词，入库到知识库。

用关键词搜全网路径（博查），每个关键词一轮。跑完看知识库新增条数。
用法（在 backend 目录）：
    .venv/Scripts/python.exe scripts/crawl_diet.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.graph import run_knowledge

KEYWORDS = [
    "减脂期饮食搭配",
    "食物热量表 减脂",
    "运动营养 蛋白质 碳水 减脂",
    "减脂餐 营养均衡 怎么吃",
    "有氧运动 减脂 饮食配合",
]


def main() -> int:
    total_in = 0
    for i, kw in enumerate(KEYWORDS, 1):
        print(f"\n[{i}/{len(KEYWORDS)}] 关键词：{kw}", flush=True)
        try:
            out = run_knowledge(trigger="manual", keyword=kw, max_per_source=6)
            c = (out.get("counts") or {})
            ins = c.get("inserted", 0)
            total_in += ins
            print(f"  状态={out.get('status')} 发现={c.get('discovered')} "
                  f"抓取={c.get('fetched')} 入库={ins} 跳过={c.get('skipped')}", flush=True)
            for e in (out.get("errors") or [])[:2]:
                print(f"  ! {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  失败：{type(e).__name__}: {e}", flush=True)
        time.sleep(3)

    print(f"\n=== 全部完成，累计入库 {total_in} 篇 ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
