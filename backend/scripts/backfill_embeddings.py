"""给知识库里的文章补语义向量 / 重算向量。

用途：
  1. 接入本地 Qwen3-Embedding 之后，把历史文章一次性补上向量；
  2. 换了 embedding 模型时全量重算（--force）；
  3. **评估入库否决阈值**：把每篇文章与各主题原型重排后的最高分打出来，
     看阈值定在哪不会误杀（--eval-veto 只打分不写库）。

    ./.venv/Scripts/python.exe scripts/backfill_embeddings.py
    ./.venv/Scripts/python.exe scripts/backfill_embeddings.py --eval-veto
    ./.venv/Scripts/python.exe scripts/backfill_embeddings.py --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description="补/重算知识库文章向量")
    ap.add_argument("--force", action="store_true", help="已有向量的也重算")
    ap.add_argument("--eval-veto", action="store_true",
                    help="只打印各文章的主题重排分，不写库（用来定阈值）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from app.database import SessionLocal, init_db
    from app.knowledge import semantics
    from app.knowledge.sources import TOPIC_PROTOTYPES
    from app.models.knowledge import Article

    init_db()
    if not semantics.available():
        print(f"语义服务不可用（{semantics.BASE_URL}）。先启动 scripts/model_server.py")
        return 1

    db = SessionLocal()
    try:
        q = db.query(Article).filter(Article.status == "ok")
        if not args.force:
            q = q.filter((Article.embedding == "") | (Article.embedding.is_(None)))
        rows = q.order_by(Article.id).all()
        if args.limit:
            rows = rows[: args.limit]

        print(f"待处理 {len(rows)} 篇，主题原型 {len(TOPIC_PROTOTYPES)} 个\n")

        if args.eval_veto:
            docs = [semantics.doc_text(a.title, a.summary, a.key_points, a.content_md)
                    for a in rows]
            best = semantics.topic_scores(docs, TOPIC_PROTOTYPES)
            if best is None:
                print("重排失败")
                return 1
            below = [a for a, s in zip(rows, best) if s < semantics.RERANK_VETO]
            print(f"否决阈值 = {semantics.RERANK_VETO}")
            print(f"会被否决 {len(below)} 篇 / 共 {len(rows)} 篇\n")
            for a, s in sorted(zip(rows, best), key=lambda x: x[1]):
                flag = "✗否决" if s < semantics.RERANK_VETO else "  保留"
                print(f"  {flag} {s:+8.3f}  [{a.domain[:22]:<22}] {(a.title or '')[:44]}")
            return 0

        # 分批算向量
        done = 0
        BATCH = 16
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            texts = [semantics.doc_text(a.title, a.summary, a.key_points, a.content_md)
                     for a in chunk]
            vecs = semantics.embed(texts)
            if not vecs or len(vecs) != len(chunk):
                print(f"  第 {i} 批向量失败，中止")
                return 1
            for a, v in zip(chunk, vecs):
                a.embedding = semantics.pack(v)
                a.embed_model = "qwen3-embedding-0.6b"
            db.commit()
            done += len(chunk)
            print(f"  已处理 {done}/{len(rows)}")

        print(f"\n完成：{done} 篇已写入向量")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
