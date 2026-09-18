"""调本地 OCR 服务（scripts/ocr_server.py），把运动海报图片识别成文本块。

设计（和知识库「语义能力是增强不是依赖」一致）：
  - 服务不可用 / 超时 → 返回 ok=False，调用方据此降级（提示用户改用手动录入）；
  - 不静默吞错：明确返回错误信息，让前端如实展示；
  - OCR 服务地址/超时可由环境变量覆盖，默认 127.0.0.1:8200 / 15s。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

OCR_SERVER_URL = os.environ.get("OCR_SERVER_URL", "http://127.0.0.1:8200")
OCR_TIMEOUT = float(os.environ.get("OCR_TIMEOUT", "15"))


def ocr_image(data: bytes) -> dict:
    """把图片字节送 OCR 服务，返回 {"ok": bool, "blocks": [...], "error": str}。

    blocks 元素：{"text": str, "conf": float, "bbox": [[x,y],...]}
    """
    payload = json.dumps(
        {"image_base64": base64.b64encode(data).decode("ascii")}
    ).encode("utf-8")
    req = urllib.request.Request(
        OCR_SERVER_URL + "/ocr",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OCR_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"OCR 服务返回 HTTP {e.code}", "blocks": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"OCR 服务不可用：{e}", "blocks": []}
