"""本地向量/重排推理服务（Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B）。

为什么单独起一个进程、而不是直接在后端里加载：
  1. 后端 venv 里没有 torch（几百 MB，不想塞进去），而本机的
     D:\\python 已经有一套带 CUDA 的 torch / transformers；
  2. 模型只在「爬取入库」和「检索」两个动作上用到，不需要和后端同生命周期；
  3. 服务挂了后端要能自动退回关键词逻辑（语义能力是增强，不是依赖）。

⚠️ 用 D:\\python\\python.exe 启动（那个解释器才有 torch）：
    D:/python/python.exe backend/scripts/model_server.py
    D:/python/python.exe backend/scripts/model_server.py --port 8100 --device auto

接口：
    GET  /health                     → 模型与设备状态
    POST /embed   {texts, query}     → {vectors, dim}
    POST /rerank  {query, documents} → {scores, threshold}
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

# ------------------------------------------------------------------ 模型路径
EMB_PATH = os.environ.get(
    "QWEN_EMB_PATH",
    r"D:\models\Qwen\Qwen3-Embedding-0___6B\models\qwen--Qwen3-Embedding-0.6B\snapshots\master",
)
RER_PATH = os.environ.get("QWEN_RER_PATH", r"D:\models\Qwen\Qwen3-Reranker-0___6B")

# 重排的重要细节（都是实测踩出来的）：
#   1. 必须左填充。右填充时 logits[:, -1] 取到的是 pad 位，判断会整个反过来。
#   2. yes/no 判定位的 token id 从 1_LogitScore/config.json 读，不靠猜。
#   3. instruction 用官方默认的那句，校准最好（实测相关文档分值为正，
#      换成自定义 instruct 后相关文档掉到负数，阈值就不好定了）。
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the '
          'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
          '<|im_end|>\n<|im_start|>user\n')
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"
DEFAULT_INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query"
TRUE_ID, FALSE_ID = 9693, 2152

# 查询侧 instruction：**默认不加**（空串）。
# 这个权重副本一旦加上官方 instruction，向量的相对次序会**变反**。用官方 model card
# 的例子实测（"What is the capital of China?" 对 中国首都 / 引力 / 法国首都 / 北京）：
#
#   Instruct: ...\nQuery: ...  → 0.7907 / 0.7038 / 0.8125 / 0.7510  法国首都排到了中国首都前面（错）
#   不加 instruction            → 0.9033 / 0.6371 / 0.8964 / 0.8145  次序正确
#
# 试过「Query: 后加空格」「只给任务描述不给 Query:」「换成中文引导句」等变体，全都不行。
# 去掉 instruction 后召回质量跃升：几个中文查询的目标文章从「排 51/75、进不了召回池」
# 变成「排 1/75、2/75」。所以默认 raw 查询编码，想复现对照实验用 QWEN_QUERY_INSTRUCT
# 环境变量或请求体里的 instruct 字段打开。
QUERY_INSTRUCT = os.environ.get("QWEN_QUERY_INSTRUCT", "").strip()

# 重排判定阈值。实测分布：真命中 +1.4 ~ +5.8，擦边 -2.6 ~ -3.7，无关 -8.3 以下。
# 取 -1.0 落在最干净的空档里（详见 app/knowledge/semantics.py 的 RERANK_KEEP）。
RERANK_THRESHOLD = -1.0

# 单条文档送重排时截断的长度（重排是逐对前向，长了线性变慢）
RERANK_DOC_CHARS = 2000
EMBED_MAX_CHARS = 1500

# 重排分批：**按 token 预算自适应**，而不是固定条数。
# 原因是交叉编码器在同一批内会 padding 到该批最长序列，长短混在一起时
# 短文本要为长文本的 padding 买单。实测（RTX 3050）：
#     8 条短要点（80 字）        0.25s
#     8 条含一条 1200 字摘要    2.63s      ← 慢 10 倍，纯浪费
# 所以先按长度升序排序、再按 token 预算装箱：批内长度接近，短文本批自然快。
#
# 但批也不能太大：实测同一批 398 条真实要点，batch=16 用 5.6 万 token、
# batch=64 用 6.5 万（padding 多 16%），批越大 padding 浪费越多。
# 真实要点中位 111 token，故预算 2048 → 每批 ~18 条，落在最优点附近。
RERANK_BATCH_TOKENS = 2048
RERANK_CHUNK_MIN = 4          # 再长也至少凑这么多条，避免全是长文时退化成逐条
RERANK_CHUNK_MAX = 16         # 一次前向的条数上限
RERANK_MAX_TOKENS = 2048      # 单对的硬截断长度
EMBED_CHUNK = 16

_lock = threading.Lock()
_state: dict = {"embed": None, "rerank": None, "tok": None, "rtok": None,
                "device": "cpu", "dtype": None, "note": ""}


def _probe_device(prefer: str) -> tuple:
    import torch

    if prefer == "cpu":
        return torch.device("cpu"), torch.float32, "强制 CPU"
    if prefer in ("auto", "cuda") and torch.cuda.is_available():
        try:
            # NVML 报 Unknown Error 时 torch.cuda.is_available() 仍可能是 True，
            # 所以真跑一次算子再决定用不用 GPU。
            x = torch.zeros(16, device="cuda") + 1.0
            torch.cuda.synchronize()
            del x
            name = torch.cuda.get_device_name(0)
            return torch.device("cuda"), torch.float16, f"CUDA: {name}"
        except Exception as e:  # noqa: BLE001
            if prefer == "cuda":
                raise
            return torch.device("cpu"), torch.float32, f"CUDA 不可用回退 CPU ({type(e).__name__})"
    return torch.device("cpu"), torch.float32, "无可用 GPU"


def load_models(prefer: str = "auto") -> None:
    import torch
    from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

    dev, dtype, note = _probe_device(prefer)
    t0 = time.time()

    etok = AutoTokenizer.from_pretrained(EMB_PATH, trust_remote_code=True)
    emdl = AutoModel.from_pretrained(EMB_PATH, torch_dtype=dtype).to(dev).eval()

    rtok = AutoTokenizer.from_pretrained(RER_PATH, trust_remote_code=True)
    rtok.padding_side = "left"          # 见文件头说明，必须左填充
    rmdl = AutoModelForCausalLM.from_pretrained(RER_PATH, torch_dtype=dtype).to(dev).eval()

    _state.update({"embed": emdl, "rerank": rmdl, "tok": etok, "rtok": rtok,
                   "device": str(dev), "dtype": str(dtype), "note": note})
    print(f"[model_server] 模型就绪 {time.time() - t0:.1f}s  device={dev} dtype={dtype} ({note})",
          flush=True)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """均值池化 + L2 归一化。

    ⚠️ 用**均值池化**，不要用 last-token —— 虽然该权重目录里
    1_Pooling/config.json 写的是 `pooling_mode_lasttoken: true`，但那是转档时标错的。
    实测（教科书对照 "What is the capital of China?" vs 正/负两段）：

        last-token : 相关 0.8805 / 无关 0.9143  → 排序**反了**
        均值       : 相关 0.9033 / 无关 0.6589  → 差值 +0.24，干净

    last-token 还会让所有相似度挤在 0.88~0.97，等于没有区分度 ——
    这正是「减脂」查询把《报废资产处置公示》排到第一的原因。
    """
    import torch

    tok, mdl, dev = _state["tok"], _state["embed"], _state["device"]
    out_vecs: list[list[float]] = []
    for i in range(0, len(texts), EMBED_CHUNK):
        chunk = [t[:EMBED_MAX_CHARS] for t in texts[i:i + EMBED_CHUNK]]
        batch = tok(chunk, padding=True, truncation=True,
                    max_length=1024, return_tensors="pt").to(dev)
        with torch.no_grad():
            hidden = mdl(**batch).last_hidden_state
        mask = batch["attention_mask"].unsqueeze(-1).float()
        vec = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        vec = torch.nn.functional.normalize(vec, p=2, dim=1)
        out_vecs.extend(vec.float().cpu().tolist())
    return out_vecs


def rerank_pairs(query: str, documents: list[str], instruct: str | None = None) -> list[float]:
    """返回每份文档的相关度分（yes/no logit 差）。结果顺序与入参一致。

    分批策略见 RERANK_BATCH_TOKENS 的注释：按长度升序装箱，批内长度相近，
    避免短文档陪长文档做 padding。
    """
    import torch

    rtok, mdl, dev = _state["rtok"], _state["rerank"], _state["device"]
    ins = instruct or DEFAULT_INSTRUCT
    if not documents:
        return []

    pairs = [
        f"{PREFIX}<Instruct>: {ins}\n<Query>: {query}\n"
        f"<Document>: {d[:RERANK_DOC_CHARS]}{SUFFIX}"
        for d in documents
    ]

    # 先量长度（只算 token，不过模型），按升序排 —— 于是每个候选窗口里
    # 最长的那条就是窗口末尾那条，用它估 token 量是保守的上界。
    lens = [len(x) for x in rtok(pairs, add_special_tokens=False)["input_ids"]]
    order = sorted(range(len(pairs)), key=lambda i: lens[i])

    out: list[float] = [0.0] * len(pairs)
    i, n = 0, len(order)
    while i < n:
        window = order[i:i + RERANK_CHUNK_MAX]
        longest = max(lens[j] for j in window) + 8
        size = max(RERANK_CHUNK_MIN,
                   min(len(window), RERANK_BATCH_TOKENS // max(1, longest)))
        batch = window[:size]
        i += size
        b = rtok([pairs[j] for j in batch], padding=True, truncation=True,
                 max_length=RERANK_MAX_TOKENS, return_tensors="pt").to(dev)
        # ⚠️ 必须传 logits_to_keep=1：只要最后一个位置的 logits。
        # 不传的话，transformers 会给**每个位置**都算 151936 维的词表 logits，
        # batch=16 × 112 长度 × 151936 × 4 字节 ≈ 1.1GB，batch=32 就要 2.2GB。
        # 4GB 显存的卡上 embed + rerank 两个模型已经占了 2.4GB，这块临时张量
        # 放不下，显存分配器反复腾挪 —— 实测同样 398 条：
        #     服务端（不带）  53s
        #     原始模型（带）   9.85s        ← 慢 5 倍全是这块显存闹的
        with torch.no_grad():
            try:
                lg = mdl(**b, logits_to_keep=1).logits[:, -1, :]
            except TypeError:                  # 老版本 transformers 用旧参数名
                lg = mdl(**b, num_logits_to_keep=1).logits[:, -1, :]
        diff = (lg[:, TRUE_ID] - lg[:, FALSE_ID]).float().cpu().tolist()
        for j, v in zip(batch, diff):
            out[j] = round(float(v), 4)
    return out


# ------------------------------------------------------------------ HTTP
# ⚠️ 请求模型必须定义在**模块级**。
# 本文件开了 `from __future__ import annotations`，注解会变成字符串，
# FastAPI 解析函数签名时要到 __globals__ 里找这个名字；写在 build_app() 内部
# 找不到，于是把 body 当成 query 参数，报 "missing loc:['query','req']"。
from pydantic import BaseModel  # noqa: E402


class EmbedReq(BaseModel):
    texts: list[str]
    query: bool = False
    # 查询侧本来要带 instruction，但实测这个权重副本加了反而把排序带反（见 QUERY_INSTRUCT 说明）。
    # 现在默认空串＝不加；传字符串可覆盖，做对照实验用。
    instruct: str | None = None


class RerankReq(BaseModel):
    query: str
    documents: list[str]
    instruct: str | None = None
    top_n: int | None = None


def build_app():
    from fastapi import FastAPI

    @asynccontextmanager
    async def lifespan(_app):
        prefer = os.environ.get("QWEN_DEVICE", "auto")
        load_models(prefer)
        yield

    app = FastAPI(title="local-model-server", lifespan=lifespan)

    @app.get("/health")
    def health():
        ok = _state["embed"] is not None and _state["rerank"] is not None
        return {"ok": ok, "device": _state["device"], "dtype": _state["dtype"],
                "note": _state["note"], "dim": 1024,
                "rerank_threshold": RERANK_THRESHOLD}

    @app.post("/embed")
    def embed(req: EmbedReq):
        texts = req.texts
        if req.query:
            ins = req.instruct if req.instruct is not None else QUERY_INSTRUCT
            if ins:                                    # 默认空串 → 原样编码（见 QUERY_INSTRUCT）
                texts = [f"Instruct: {ins}\nQuery:{t}" for t in texts]
        with _lock:
            vecs = embed_texts(texts)
        return {"vectors": vecs, "dim": len(vecs[0]) if vecs else 0}

    @app.post("/rerank")
    def rerank(req: RerankReq):
        if not req.documents:
            return {"scores": [], "threshold": RERANK_THRESHOLD}
        with _lock:
            scores = rerank_pairs(req.query, req.documents, req.instruct)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        if req.top_n:
            order = order[:req.top_n]
        return {"scores": scores, "order": order, "threshold": RERANK_THRESHOLD}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 Qwen3 向量/重排服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    args = ap.parse_args()

    os.environ["QWEN_DEVICE"] = args.device
    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
