"""语义检索回归评估：改完模型服务 / 阈值 / 文档文本之后，跑这个。

为什么要它：重排分是**绝对分**，阈值（`semantics.RERANK_KEEP`）只能靠实测分布来定。
换模型、改指令、动文档文本，分数尺度都会变，阈值就必须重新校准 —— 这个脚本
用一批标了「该有结果 / 该为空」的查询把分布打出来，一眼看出阈值定得合不合适。

它**不写库**，只打分。用法（在后端 venv 下，且本地模型服务已在 8100 起好）：

    cd backend
    ./.venv/Scripts/python.exe scripts/eval_semantics.py
    ./.venv/Scripts/python.exe scripts/eval_semantics.py --threshold -1
    ./.venv/Scripts/python.exe scripts/eval_semantics.py --verbose
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                      # noqa: E402
from app.knowledge import retrieve as R                    # noqa: E402
from app.knowledge import semantics as S                   # noqa: E402
from app.models.knowledge import Article                   # noqa: E402

# 「该有结果」：知识库里确实有对题的文章
SHOULD_HIT = [
    "减脂期怎么做有氧",
    "力量训练每周练几次",
    "跑步时心率多少合适",
    "每周该跑多少公里",
    "运动后怎么拉伸",
    "运动前要不要热身",
    "有氧和力量训练怎么安排",
    # ↓ 睡眠这三条是**专门补进来锁回归**的，不是随手加的。
    # 它们曾经全部返空，而且原因各不相同，正好覆盖三个独立的失效点：
    #   1. 「怎么提高睡眠质量」——重排整段喂入时被主话题淹没
    #      （《Sleep and Immunity》六条要点里五条讲免疫，整段 -6.02，
    #       单独打那条睡眠卫生要点 +4.34）。要点级打分取最大才修好。
    #   2. 「睡眠不足会影响免疫力吗」——5000 字抽取窗口把中段吃掉了，
    #      内容根本没进摘要/要点。分段抽取 + 历史重抽才修好。
    #   3. 「接种疫苗前要不要睡好」——同上，内容来自恢复的中段。
    # 另外还补了 /sleep-hygiene 栏目页，库里才有纯睡眠卫生文章。
    # 这三条现在能过，全靠上面这几件事；任何一件回退，这里就会红。
    "怎么提高睡眠质量",
    "睡眠不足会影响免疫力吗",
    "接种疫苗前要不要睡好",
]

# 「该为空」：主题完全在知识库之外，返回任何东西都是噪声。
# 这几条是最容易出问题的：模型倾向于「矮子里拔将军」，
# 不设阈值时问「股市行情」会捞回一篇流感疫苗科普。
SHOULD_MISS = [
    "股市今天行情怎么样",
    "怎么写一个 Python 爬虫",
    "明天天气如何",
    "北京到上海高铁要多久",
]


def _pool_and_scores(db, query: str):
    """复现 retrieve 里的召回 + 重排，但把 30 篇全打出来（方便看分布）。"""
    articles = [a for a in db.query(Article).filter(Article.status == "ok").all() if a.embedding]
    if not articles:
        return None
    qv = S.embed([query], query=True)
    if not qv:
        return None
    scored = sorted(((S.cosine(qv[0], S.unpack(a.embedding)), a) for a in articles),
                    key=lambda x: -x[0])
    pool = R._recall_pool(query, scored)
    ranks = R._probe_ranks(query, pool)
    if ranks is None:
        return None
    order = sorted(zip(ranks, pool), key=lambda x: -x[0])
    return order


def main() -> int:
    ap = argparse.ArgumentParser(description="语义检索回归评估")
    ap.add_argument("--threshold", type=float, default=S.RERANK_KEEP,
                    help=f"要评估的阈值（默认取当前配置 {S.RERANK_KEEP}）")
    ap.add_argument("--verbose", action="store_true", help="每条查询打印召回池前 5 名")
    args = ap.parse_args()

    st = S.status()
    print(f"语义服务：{'可用' if st['available'] else '**不可用**（先起 model_server.py）'}  {st['url']}")
    if not st["available"]:
        return 2
    print(f"评估阈值：{args.threshold}\n")

    db = SessionLocal()
    hit_ok = hit_total = 0
    miss_ok = miss_total = 0
    hit_best: list[float] = []
    miss_best: list[float] = []

    print("== 该有结果 ==")
    for q in SHOULD_HIT:
        order = _pool_and_scores(db, q)
        if order is None:
            print(f"  {q:20} 语义不可用")
            continue
        hit_total += 1
        top = order[0]
        keep = [r for r, _ in order if r >= args.threshold]
        hit_best.append(top[0])
        ok = bool(keep)
        hit_ok += ok
        flag = "✓" if ok else "✗ 一条都没过阈值"
        print(f"  {q:20} best={top[0]:+7.3f} 过线 {len(keep):2} 篇 {flag}")
        print(f"      → {top[1][1].title[:56]}")
        if args.verbose:
            for r, (cos, a) in order[1:5]:
                print(f"        {r:+7.3f} cos={cos:+.3f} {a.title[:48]}")

    print("\n== 该为空 ==")
    for q in SHOULD_MISS:
        order = _pool_and_scores(db, q)
        if order is None:
            print(f"  {q:20} 语义不可用")
            continue
        miss_total += 1
        top = order[0]
        keep = [r for r, _ in order if r >= args.threshold]
        miss_best.append(top[0])
        ok = not keep
        miss_ok += ok
        flag = "✓ 正确返空" if ok else f"✗ 混进 {len(keep)} 篇"
        print(f"  {q:20} best={top[0]:+7.3f} {flag}")
        print(f"      → {top[1][1].title[:56]}")

    if hit_best and miss_best:
        print(f"\n分数分布：该命中 {min(hit_best):+.2f} ~ {max(hit_best):+.2f}"
              f"  |  该为空 {min(miss_best):+.2f} ~ {max(miss_best):+.2f}")
        gap = (max(miss_best), min(hit_best))
        if gap[0] < gap[1]:
            print(f"空档：{gap[0]:+.2f} ~ {gap[1]:+.2f}"
                  f"  → 阈值落在区间内即可（当前 {args.threshold}）")
        else:
            print("⚠ 两类分数重叠，没有干净阈值 —— 该收紧文档文本或换指令了")

    print(f"\n命中 {hit_ok}/{hit_total}，正确返空 {miss_ok}/{miss_total}")
    return 0 if hit_ok == hit_total and miss_ok == miss_total else 1


if __name__ == "__main__":
    sys.exit(main())
