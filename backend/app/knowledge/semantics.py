"""本地语义能力：向量召回 + 重排精排。

模型是 Qwen3-Embedding-0.6B 与 Qwen3-Reranker-0.6B，跑在独立的本地服务里
（`scripts/model_server.py`，用带 CUDA 的解释器启动，见那里的说明）。

**核心纪律：语义能力是增强，不是依赖。**
服务没启动、超时、报错、返回空，一律静默退回关键词逻辑 —— 知识库检索和入库
不能因为一个本地服务没开就整个瘫掉。所有对外函数返回 None 就代表「不可用，走老路」。
"""
from __future__ import annotations

import json
import math
import os
import time

import httpx

BASE_URL = os.environ.get("MODEL_SERVER_URL", "http://127.0.0.1:8100").rstrip("/")
EMBED_TIMEOUT = float(os.environ.get("MODEL_EMBED_TIMEOUT", "120"))
RERANK_TIMEOUT = float(os.environ.get("MODEL_RERANK_TIMEOUT", "60"))
# 置 0 可强制关掉语义能力（对比测试用）
ENABLED = os.environ.get("MODEL_SERVER_ENABLED", "1") not in ("0", "false", "False")

EMBED_DIM = 1024
# 入库时单次请求最多算多少条向量，避免一次灌几百条把服务拖住
EMBED_CHUNK = 32
# 重排时的候选池大小：先按向量召回这么多，再交给重排精排
RECALL_POOL = 30
# 重排判定阈值。实测（80 篇文章、10 个该有结果的查询 + 4 个该为空的查询）分布：
#   真命中    +1.19 ~ +5.73
#   擦边      -2.6 ~ -3.7（话题挨着但不回答问题的，例如问睡眠捞到泛体能文章）
#   无关      -8.19 ~ -10.6
# 最初取 -1.0：离真命中下限和擦边上限各留约 2 分余量，落在最干净的空档里。
#
# ⚠️ 2026-09-18 下调到 -2.5：上面那份分布是**训练类语料**标定的。知识库补进饮食/
# 食材替换类内容后，这类文章的 rerank 分会**系统性偏低**（实测「减肥代替蔬菜的食物」
# 的真命中只有 -1.91，「教你食物互换」更只有 -2.4 左右），-1.0 会把它们全挡掉，
# 表现为「明明有对口文章却 0 命中」。放宽到 -2.5 后：饮食类能捞回、训练类不受影响、
# 无关查询（"今天天气怎么样"）仍稳定 0 命中。
# 改模型/大改语料组成后，用 scripts/eval_semantics.py 重新标定。
RERANK_KEEP = float(os.environ.get("MODEL_RERANK_KEEP", "-2.5"))
# 入库否决阈值：比检索阈值更保守。英文文章在同为相关时分会系统性偏低一点
# （实测中文相关 +4.8 / 英文相关 -2.8），所以否决线放到 -6，
# 只在「六个主题原型都觉得不相关」时才丢弃。
RERANK_VETO = float(os.environ.get("MODEL_RERANK_VETO", "-6.0"))

_client: httpx.Client | None = None
_health = {"ts": 0.0, "ok": False}


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(RERANK_TIMEOUT, connect=3.0))
    return _client


def available(ttl: float = 30.0) -> bool:
    """服务是否可用。带 TTL 缓存，避免每篇文章都去探一次。"""
    if not ENABLED:
        return False
    now = time.time()
    if now - _health["ts"] < ttl:
        return bool(_health["ok"])
    ok = False
    try:
        r = _get_client().get(f"{BASE_URL}/health", timeout=3.0)
        ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:  # noqa: BLE001
        ok = False
    _health.update({"ts": now, "ok": ok})
    return ok


def status() -> dict:
    """给接口/前端看的语义能力状态。"""
    up = available(ttl=0)
    return {"enabled": ENABLED, "available": up, "url": BASE_URL,
            "dim": EMBED_DIM, "rerank_keep": RERANK_KEEP}


def embed(texts: list[str], query: bool = False) -> list[list[float]] | None:
    """算向量。

    query=True 只是把「这是查询侧」告诉服务端，**默认不会再加 instruction** ——
    服务端实测发现这个权重副本加 instruction 会把排序带反（详见 model_server.py
    里的 QUERY_INSTRUCT 说明）。调用方不用关心，照常传 query=True 即可。
    """
    if not texts or not available():
        return None
    out: list[list[float]] = []
    try:
        for i in range(0, len(texts), EMBED_CHUNK):
            chunk = texts[i:i + EMBED_CHUNK]
            r = _get_client().post(f"{BASE_URL}/embed",
                                   json={"texts": chunk, "query": query},
                                   timeout=EMBED_TIMEOUT)
            r.raise_for_status()
            vecs = r.json().get("vectors") or []
            if len(vecs) != len(chunk):
                return None
            out.extend(vecs)
    except Exception:  # noqa: BLE001
        return None
    return out or None


def rerank(query: str, documents: list[str]) -> list[float] | None:
    """重排打分（yes/no logit 之差）。分数越高越相关，< RERANK_KEEP 视为不相关。"""
    if not query or not documents or not available():
        return None
    try:
        r = _get_client().post(f"{BASE_URL}/rerank",
                               json={"query": query, "documents": documents},
                               timeout=RERANK_TIMEOUT)
        r.raise_for_status()
        scores = r.json().get("scores") or []
        return scores if len(scores) == len(documents) else None
    except Exception:  # noqa: BLE001
        return None


def relevant(query: str, documents: list[str]) -> list[bool] | None:
    """批量判定是否相关。返回 None 表示服务不可用。"""
    scores = rerank(query, documents)
    if scores is None:
        return None
    return [s >= RERANK_KEEP for s in scores]


def topic_scores(documents: list[str], prototypes: list[str]) -> list[float] | None:
    """每篇文档与各「主题原型」重排后取最大值 —— 即「最像哪一类该收的内容」。

    比单个原型稳：一篇讲运动营养的文章对「营养原型」分高、对「力量训练原型」低，
    取最大值才不会因为原型选错而误杀。
    """
    if not documents or not prototypes or not available():
        return None
    best = [-1e9] * len(documents)
    for p in prototypes:
        scores = rerank(p, documents)
        if scores is None:
            return None
        for i, s in enumerate(scores):
            if s > best[i]:
                best[i] = s
    return best


# ------------------------------------------------------------------ 文本组装
def doc_text(title: str = "", summary: str = "", key_points=None,
             content: str = "", limit: int = 1200) -> str:
    """拼出用来算向量 / 送重排的文章文本。

    用「标题 + 摘要 + 要点」而不是全文：检索要的是「这篇在讲什么」，
    不是它的每个字。摘要为空（LLM 抽取降级）时才退回正文开头。
    向量和重排用同一份文本，保证两者看的是同一个东西。
    """
    parts = [str(title or "").strip()]
    if summary:
        parts.append(str(summary).strip())
    if key_points:
        parts.append("；".join(str(p) for p in key_points[:6]))
    text = "\n".join(p for p in parts if p)
    if len(text) < 60 and content:      # 摘要缺失时兜底
        text = f"{text}\n{content.strip()[:limit]}"
    return text[: limit + 400].strip()


def rerank_text(title: str = "", summary: str = "", key_points=None,
                content: str = "", limit: int = 1200) -> str:
    """专供**重排**的文档文本：**不放标题**，用摘要 + 要点。

    标题对重排只有副作用，而且是系统性的。同一个查询、同一篇文章，实测：

        q=怎么提高睡眠质量    标题+摘要 -9.32 ／ 摘要+标题 -7.31 ／ **仅摘要 -2.97**
        q=力量训练每周练几次  标题+摘要 +3.73 ／ **仅摘要 +7.05**
        q=how to improve sleep quality（英文查询）  标题+摘要 -9.17 ／ **仅摘要 -5.34**

    中英文查询、中英文文档都一样：加标题分就掉，英文标题尤其致命（单独送英文标题
    能到 -13）。阈值是 -4，有没有标题直接决定这篇能不能进结果。猜是标题那种短语式
    写法跟重排模型训练时的「段落」形态不匹配。

    为什么只对重排去标题、向量侧不去：向量侧反过来 —— 带标题的召回排名更好或持平，
    而且去掉要全量重算向量。两边各用各的最优文本，不强行统一。

    摘要缺失（LLM 抽取降级）时才退回标题 + 正文开头。
    """
    s = str(summary or "").strip()
    kp = "；".join(str(p) for p in (key_points or [])[:4])
    if s:
        return f"{s}\n{kp}".strip()[: limit + 400]
    head = str(title or "").strip()
    return f"{head}\n{str(content or '').strip()[:limit]}".strip()


# ------------------------------------------------------------------ 向量存取
def pack(vec: list[float]) -> str:
    """存库用。float 保留 5 位小数，够用且能把体积压掉一半。"""
    return json.dumps([round(float(x), 5) for x in vec], separators=(",", ":"))


def unpack(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) and len(v) == EMBED_DIM else None
    except Exception:  # noqa: BLE001
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """两个已归一化向量的余弦（即点积）。未归一化时也能算，只是慢一点。"""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = na = nb = 0.0
    for i in range(n):
        x, y = a[i], b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
