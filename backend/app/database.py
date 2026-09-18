from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, future=True)

if _is_sqlite:
    # SQLite 默认关闭外键，不开这个 PRAGMA，ON DELETE CASCADE 形同虚设
    @event.listens_for(Engine, "connect")
    def _sqlite_pragma(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_sqlite_columns():
    """SQLite 轻量迁移：补上后加的列，避免每次改模型都要重建库。"""
    if not _is_sqlite:
        return
    from sqlalchemy import inspect, text

    wanted = {
        "activities": [("hr_zone_json", "JSON")],
        "training_plans": [("evidence_json", "JSON")],
        # 本地 Qwen3-Embedding 的向量（JSON 数组，1024 维）
        "kb_articles": [("embedding", "TEXT"), ("embed_model", "TEXT")],
    }

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    for table, cols in wanted.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name not in have:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def seed_sources():
    """把权威源清单灌进 kb_sources。

    - 清单里没有的源：不动（用户自己加的源不该被清掉）
    - 清单里新增的：插入
    - 已存在的：同步站点定义，但保留 enabled 与 last_crawled_at
    - 进了 DISABLED_SOURCES 的：置为禁用并写明原因。
      这解决的是「清单改了但库里还留着旧的、且已经证明抓不通」的问题 ——
      不然每周任务都会在这些源上白跑一遍。
    """
    from app.knowledge.sources import DISABLED_SOURCES, SOURCES
    from app.models.knowledge import Source

    db = SessionLocal()
    try:
        existing = {s.domain: s for s in db.query(Source).all()}

        for raw in SOURCES:
            cur = existing.get(raw["domain"])
            if cur is None:
                db.add(Source(**raw))
            else:
                # 清单更新时同步站点定义，但保留 enabled 与 last_crawled_at
                for k in ("name", "list_urls", "lang", "category", "authority", "note"):
                    if k in raw:
                        setattr(cur, k, raw[k])

        for raw in DISABLED_SOURCES:
            cur = existing.get(raw["domain"])
            if cur is not None and cur.enabled:
                cur.enabled = False
                cur.note = f"已停用：{raw['blocked_by']}。" + raw.get("note", "")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    from app.models import fitness, knowledge  # noqa: F401  注册表

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
    seed_sources()
