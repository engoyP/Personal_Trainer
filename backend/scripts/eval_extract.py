"""分段抽取覆盖率回归：改了切分/去噪/噪声小节规则之后，跑这个。

为什么需要它：既然下游只看摘要和要点，那「摘要和要点能覆盖多少正文」就等于
这篇文章的全部可见度。历史上这里翻过两次车：

  1. 早期「头 4000 + 中间省略 + 尾 1000」—— 库里 64% 的文章被截断，
     一篇 52949 字的《Sleep and Immunity》有 90.6% 的正文没进过模型。
  2. 改成按 `##` 切块后，没有 markdown 子标题的文章（如 ACSM 页面）被当成
     一整块、再被 `_trim` 砍掉 —— 覆盖率掉到 59%。

两次都是**静默**的：不报错、不失败，只是内容悄悄变少。所以必须有脚本盯着。

它**只算切分，不调 LLM**（零成本、秒级出结果），因此可以随时跑，
包括改代码的中间过程。它保护的是「内容有没有被切出去」，
至于「切进去的内容有没有被提炼好」，那是 `eval_semantics.py` 的事。

用法（后端 venv）：

    cd backend
    ./.venv/Scripts/python.exe scripts/eval_extract.py
    ./.venv/Scripts/python.exe scripts/eval_extract.py --domain sleepfoundation.org
    ./.venv/Scripts/python.exe scripts/eval_extract.py --min 0.95 --verbose
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                       # noqa: E402
from app.knowledge.extract import (                         # noqa: E402
    MAX_SEGMENTS, SEG_CHARS, _hard_split, _segment, _split_blocks,
)
from app.knowledge.scoring import _prose_only               # noqa: E402
from app.models.knowledge import Article                    # noqa: E402

# 及格线。当前全库最低 99%，留 5 个点的余量给新源（排版没见过的文章）。
DEFAULT_MIN = 0.95


def coverage(title: str, content: str) -> dict:
    """算一篇文章的切分覆盖率。

    分母是「有效正文 − 噪声小节」：去噪剔掉图片行和链接密集行，
    `_split_blocks` 再剔掉 `## References` 这类整块非正文。
    这两类本来就不该进模型，不算「被丢掉的内容」。
    """
    prose = _prose_only(content or "")
    if not prose:
        return {"ok": False, "reason": "去噪后正文为空"}

    denom = sum(len(b) for b in _split_blocks(prose))
    if denom <= 0:
        return {"ok": False, "reason": "去噪后无可切块"}

    segs = _segment(prose)
    covered = sum(len(s) for s in segs)
    # 段与段之间拼接时多了 "\n\n"，所以 covered 会略微超过 denom；
    # 那是分隔符不是内容，夹到 1.0 再报，免得出现「覆盖率 100.3%」这种怪数字。
    return {
        "ok": True,
        "raw": len(content or ""),
        "prose": len(prose),
        "denom": denom,
        "covered": covered,
        "ratio": min(1.0, covered / denom),
        "segs": len(segs),
        "noise": 1 - len(prose) / max(1, len(content or "")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="分段抽取覆盖率回归")
    ap.add_argument("--domain", help="只看某个源")
    ap.add_argument("--min", type=float, default=DEFAULT_MIN, help=f"及格线（默认 {DEFAULT_MIN}）")
    ap.add_argument("--verbose", action="store_true", help="逐篇打印")
    ap.add_argument("--limit", type=int, default=0, help="只看前 N 篇")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Article).filter(Article.status == "ok")
        if args.domain:
            q = q.filter(Article.domain == args.domain)
        arts = q.all()
        if args.limit:
            arts = arts[: args.limit]
    finally:
        db.close()

    if not arts:
        print("没有文章可测")
        return 1

    rows, skipped = [], 0
    for a in arts:
        r = coverage(a.title, a.content_md)
        if not r["ok"]:
            skipped += 1
            continue
        r["title"] = a.title
        r["domain"] = a.domain
        rows.append(r)

    rows.sort(key=lambda x: x["ratio"])
    ratios = [r["ratio"] for r in rows]
    n = len(ratios)

    print(f"文章 {len(arts)} 篇（可测 {n} 篇，跳过 {skipped} 篇）")
    print(f"参数：SEG_CHARS={SEG_CHARS}  MAX_SEGMENTS={MAX_SEGMENTS}")
    print()

    if args.verbose:
        print(f"{'覆盖率':>7} {'段数':>4} {'有效正文':>8} {'原文':>8} {'噪声占比':>7}  标题")
        for r in rows:
            flag = "" if r["ratio"] >= args.min else "  ← 不及格"
            print(f"{r['ratio']:>7.1%} {r['segs']:>4} {r['denom']:>8} {r['raw']:>8} "
                  f"{r['noise']:>7.1%}  {r['title'][:34]}{flag}")
        print()

    def pct(xs, p):
        if not xs:
            return 0.0
        return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]

    noises = [r["noise"] for r in rows]
    print(f"覆盖率：最低 {min(ratios):.1%}  中位 {pct(ratios, 0.5):.1%}  "
          f"p90 {pct(ratios, 0.9):.1%}")
    # 噪声占比在长文和短文上差一个量级：短文几乎没噪声，CDC/Sleep Foundation
    # 那种带大量图片行和链接块的能到 70%+。只看中位数会低估去噪的价值。
    print(f"噪声占比（图片行 + 链接密集行）：p25 {pct(noises, 0.25):.1%}  "
          f"中位 {pct(noises, 0.5):.1%}  p75 {pct(noises, 0.75):.1%}  最高 {max(noises):.1%}")
    multi = sum(1 for r in rows if r["segs"] > 1)
    print(f"走多段抽取的：{multi}/{n} 篇（其余仍走单次调用）")

    bad = [r for r in rows if r["ratio"] < args.min]
    if bad:
        print(f"\n✗ {len(bad)} 篇低于及格线 {args.min:.0%}：")
        for r in bad[:10]:
            print(f"  {r['ratio']:>6.1%}  {r['title'][:60]}")
        return 1
    print(f"\n✓ 全部 ≥ {args.min:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
