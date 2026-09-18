"""文章图片 OCR：把正文里的图片下载下来，用 EasyOCR 提取图片上的**文字**。

用途：有些教学内容的要点以图片（图解、图注）呈现，纯文本抽取会漏掉这部分。
**边界**：EasyOCR 只能识别图片里的文字（比如「动作1：站姿股四头肌拉伸」这类
图注），看不懂姿势动作本身——理解图片内容需要多模态视觉模型。

纪律：
  - OCR 服务（8200）不可用 → 静默跳过，不拖垮爬取主流程；
  - 每篇最多 OCR N 张图（CPU 模式每张 10s+，不限制会把一轮爬取拖到几十分钟）；
  - 只补进正文，不动 LLM 抽取的入参逻辑（抽出来的要点由现有 extract 环节决定）。
"""
from __future__ import annotations

import os
import re

import httpx

from app.services.ocr_client import OCR_SERVER_URL

# 每篇最多处理几张图：CPU 模式每张 10s+，3 张已是 30s/篇
MAX_IMAGES = int(os.environ.get("KB_IMAGE_OCR_MAX", "3"))
MAX_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT = 20.0
# 噪声过滤：logo/装饰图的 OCR 结果置信度低、有效字符少
MIN_AVG_CONF = float(os.environ.get("KB_IMAGE_OCR_MIN_CONF", "0.5"))
MIN_VALID_CHARS = 8

_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def extract_image_urls(md: str, limit: int = MAX_IMAGES) -> list[str]:
    """从 markdown 里提取图片 URL（去重、限量）。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in _IMG_RE.findall(md or ""):
        u = u.strip()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def ocr_urls(urls: list[str], timeout: float = 90.0) -> list[str]:
    """下载图片并 OCR，返回识别出的文字（去空、去重）。失败单张跳过。"""
    import base64
    import json
    import urllib.request

    texts: list[str] = []
    for u in urls:
        try:
            r = httpx.get(u, timeout=FETCH_TIMEOUT, headers={"User-Agent": _UA},
                          follow_redirects=True)
            if r.status_code != 200 or not r.content or len(r.content) > MAX_BYTES:
                continue
            payload = json.dumps(
                {"image_base64": base64.b64encode(r.content).decode("ascii")}
            ).encode("utf-8")
            req = urllib.request.Request(
                OCR_SERVER_URL + "/ocr", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                blocks = (json.loads(resp.read().decode("utf-8")) or {}).get("blocks") or []
            if not blocks:
                continue
            # logo/装饰图会被 OCR 出乱码（如「睛68阢 A1434e5I2」），置信度普遍偏低。
            # 用平均置信度 + 最小长度挡掉，不给正文掺噪声。
            avg_conf = sum(float(b.get("conf") or 0) for b in blocks) / len(blocks)
            txt = " ".join((b.get("text") or "").strip() for b in blocks).strip()
            valid = sum(1 for c in txt if c.isalnum() or "\u4e00" <= c <= "\u9fff")
            if avg_conf < MIN_AVG_CONF or valid < MIN_VALID_CHARS:
                continue
            if txt and txt not in texts:
                texts.append(txt)
        except Exception:  # noqa: BLE001
            continue
    return texts


def enrich(fetched: list[dict], max_articles: int = 20) -> int:
    """给抓取结果补图片 OCR 文字：追加进 markdown，返回 OCR 成功张数。

    追加进正文（而不是单独字段）是为了让下游 LLM 抽取自动看到 —— 图解类内容
    的文字往往正是要点所在，抽不出来等于这篇白抓。
    """
    n = 0
    for f in fetched[:max_articles]:
        if not f.get("ok"):
            continue
        md = f.get("markdown") or ""
        urls = extract_image_urls(md)
        if not urls:
            continue
        texts = ocr_urls(urls)
        if texts:
            block = "\n\n## 图片文字（OCR 提取）\n" + "\n".join(f"- {t}" for t in texts)
            f["markdown"] = md + block
            n += len(texts)
    return n
