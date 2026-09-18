"""重跑历史文章的 LLM 抽取，刷新摘要 / 要点 / 向量。

为什么需要它：`summary` 和 `key_points` 只在爬取时抽一次、之后不再重算，
而它们是向量召回、重排、喂给教练的**唯一**文本来源。抽取策略一变
（比如从「头 4000 + 尾 1000 截断」改成「分段抽取」），历史文章不会
自动受益 —— 得显式重跑一遍。

默认只重抽「有效正文超过 SEG_CHARS」的文章，也就是真正被截断过的那批；
其余文章的抽取结果不会因为分段策略而改变，没必要花 token 重跑。

    # 先看会重抽哪些、会变成什么样（不写库）
    ./.venv/Scripts/python.exe scripts/reextract.py --dry-run
    # 真跑
    ./.venv/Scripts/python.exe scripts/reextract.py
    # 指定源、限制条数、单线程
    ./.venv/Scripts/python.exe scripts/reextract.py --domain sleepfoundation.org --limit 3 --workers 1
    # 全量重抽（含短文）+ 跳过向量重算
    ./.venv/Scripts/python.exe scripts/reextract.py --all --no-embed
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pick(db, *, all_articles: bool, min_chars: int, domain: str | None,
         limit: int | None):
    from app.knowledge.extract import _prose_only, _segment
    from app.models.knowledge import Article

    rows = []
    q = db.query(Article).filter(Article.status == "ok")
    if domain:
        q = q.filter(Article.domain == domain)
    for a in q.all():
        prose = _prose_only(a.content_md or "")
        if not all_articles and len(prose) <= min_chars:
            continue
        rows.append((len(prose), len(_segment(prose)), a))
    rows.sort(key=lambda r: -r[0])
    if limit:
        rows = rows[:limit]
    return rows


def reextract_one(a) -> tuple:
    """返回 (article_id, 新字段, 旧摘要, 旧要点数)。异常不外抛，交给调用方统计。"""
    from app.knowledge.extract import extract

    f = extract(a.title or "", a.content_md or "", a.published_at, a.domain or "")
    fields = {
        "summary": f.get("summary") or "",
        "key_points": f.get("key_points") or [],
        "topics": f.get("topics") or [],
        "category": f.get("category") or a.category,
        "evidence_level": f.get("evidence_level") or a.evidence_level,
        "organization": f.get("organization") or a.organization,
    }
    return a.id, fields, a.summary or "", len(a.key_points or []), f.get("extract_note") or ""


def main() -> int:
    ap = argparse.ArgumentParser(description="重跑历史文章的抽取与向量")
    ap.add_argument("--all", action="store_true", help="重抽全部文章（默认只重抽被截断的）")
    ap.add_argument("--min-chars", type=int, default=None,
                    help="有效正文超过多少字才重抽（默认取 extract.SEG_CHARS）")
    ap.add_argument("--domain", default=None, help="只处理某个域名")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少篇")
    ap.add_argument("--workers", type=int, default=4, help="并发数，默认 4（对齐 EXTRACT_CONCURRENCY）")
    ap.add_argument("--dry-run", action="store_true", help="只列出会重抽的文章，不改库")
    ap.add_argument("--no-embed", action="store_true", help="跳过向量重算")
    ap.add_argument("--verbose", action="store_true", help="逐篇打印新旧摘要")
    args = ap.parse_args()

    from app.database import SessionLocal, init_db
    from app.knowledge.extract import SEG_CHARS

    min_chars = args.min_chars if args.min_chars is not None else SEG_CHARS
    init_db()
    db = SessionLocal()
    try:
        rows = pick(db, all_articles=args.all, min_chars=min_chars,
                    domain=args.domain, limit=args.limit)
        if not rows:
            print("没有需要重抽的文章。")
            return 0

        total = sum(r[0] for r in rows)
        print(f"待重抽 {len(rows)} 篇（有效正文合计 {total} 字，"
              f"按 {args.workers} 并发约需 {len(rows) * 4 // max(1, args.workers)} 秒）")
        if args.dry_run:
            print()
            print("%8s %4s  %s" % ("有效正文", "段数", "标题"))
            for n, seg_n, a in rows:
                print("%8d %4d  %s" % (n, seg_n, (a.title or "")[:64]))
            print("\n--dry-run：未写库。去掉该参数即执行。")
            return 0

        # 先算出所有新结果，再统一写库 —— 中途失败不会留下半新半旧的库。
        results: list[tuple] = []
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(reextract_one, a) for _, _, a in rows]
            for i, fut in enumerate(futures, 1):
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print(f"  [{i}/{len(rows)}] 抽取异常：{type(e).__name__}: {e}")
                if i % 5 == 0 or i == len(rows):
                    print(f"  已抽取 {i}/{len(rows)}")

        by_id = {a.id: a for _, _, a in rows}
        changed = 0
        for aid, fields, old_sum, old_n, note in results:
            a = by_id.get(aid)
            if a is None:
                continue
            if a.summary == fields["summary"] and (a.key_points or []) == fields["key_points"]:
                if args.verbose:
                    print(f"  = 无变化  {a.title[:56]}")
                continue
            if args.verbose:
                print()
                print("+" + "=" * 78)
                print(f"| {(a.title or '')[:76]}")
                print("+" + "=" * 78)
                print(f"  旧（{old_n} 条要点）：{old_sum[:160]}")
                print(f"  新（{len(fields['key_points'])} 条要点）：{fields['summary'][:160]}")
                for p in fields["key_points"]:
                    print(f"      - {p[:100]}")
            a.summary = fields["summary"]
            a.key_points = fields["key_points"]
            a.topics = fields["topics"]
            a.category = fields["category"]
            a.evidence_level = fields["evidence_level"]
            a.organization = fields["organization"]
            # 摘要/要点变了 → 向量必须重算，否则检索用的是过期语义
            a.embedding = None
            a.embed_model = ""
            changed += 1
        db.commit()

        print()
        print(f"写库完成：{changed} 篇更新，{len(results) - changed} 篇无变化，{failed} 篇失败")

        if args.no_embed or not changed:
            if changed:
                print("--no-embed：向量未重算，检索会用到过期向量，记得补跑。")
            return 0

        from app.knowledge import semantics
        from app.knowledge.pipeline import _attach_embeddings
        from app.models.knowledge import Article

        if not semantics.available(ttl=0):
            print("模型服务不可用：向量未重算。语义检索会退回关键词路径，"
                  "服务起来后重跑本脚本即可补齐。")
            return 0
        pending = [a for a in db.query(Article).all() if not a.embedding]
        if not pending:
            print("没有待补向量的文章。")
            return 0
        print(f"重算向量：{len(pending)} 篇 ...")
        n = _attach_embeddings(db, pending)
        print(f"向量重算完成：{n} 篇")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
