"""本地 OCR 推理服务（EasyOCR）。独立进程，原生 http.server 实现（不依赖 fastapi）。

为什么独立进程 + 用继承 D:\\python 的 ocr_venv 跑：
  - easyocr 依赖 torch（已在 D:\\python，ocr_venv 继承它，不重复下载）；
  - OCR 只在「识图录入」时用，不必和后端同生命周期；
  - 服务挂了 / 没装好，后端 /recognize 会静默降级（提示手动录入）。
  - 用标准库 http.server 而不是 fastapi，省掉额外依赖。

启动：
    D:/personal-toolbox/backend/ocr_venv/Scripts/python.exe backend/scripts/ocr_server.py --port 8200

接口：
    GET  /health                  -> {"ok": bool, "device": str, "note": str}
    POST /ocr  JSON {"image_base64": str}
                                  -> {"ok": bool, "blocks": [{"text","conf","bbox"}], "count": int}
"""
from __future__ import annotations

import argparse
import base64
import gc
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

# 空闲多久（秒）自动释放模型显存。OCR 每天只识图一两次，常驻白占显存，
# 会挤掉语义服务 rerank 的显存导致生成计划卡死，所以用完自动放。
IDLE_RELEASE_SEC = float(os.environ.get("OCR_IDLE_RELEASE_SEC", "600"))

_STATE = {"reader": None, "device": "cpu", "note": "idle（模型未加载）",
          "ready": False, "last_used": 0.0}
_LOCK = threading.Lock()          # 保护 _STATE 读写
_LOAD_LOCK = threading.Lock()     # 防止并发触发重复加载


def load_model() -> bool:
    """懒加载 EasyOCR。返回是否就绪。并发调用只会真正加载一次。"""
    if _STATE["ready"]:
        return True
    with _LOAD_LOCK:
        if _STATE["ready"]:
            return True
        _STATE["note"] = "loading..."
        try:
            import easyocr

            use_cuda = os.environ.get("OCR_CUDA", "1") not in ("0", "false", "")
            t0 = time.time()
            reader = easyocr.Reader(["ch_sim", "en"], gpu=use_cuda)
            _STATE.update({"reader": reader, "device": "cuda" if use_cuda else "cpu",
                           "ready": True, "note": "EasyOCR ch_sim+en"})
            print("[ocr_server] EasyOCR ready gpu=%s %.1fs"
                  % (use_cuda, time.time() - t0), flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            _STATE.update({"ready": False, "note": "load failed: %s" % e})
            print("[ocr_server] load failed:", e, flush=True)
            return False


def release_model() -> None:
    """释放模型，把显存还给系统（语义服务的 rerank 用得上）。

    只 `del reader` 不够：EasyOCR 的 Reader 内部 detector/recognizer 各自持有
    torch 模型，必须显式置空再 gc，否则权重还挂在显存上（实测只降 150MB）。
    """
    with _LOCK:
        reader = _STATE["reader"]
        _STATE["reader"] = None
        _STATE["ready"] = False
        _STATE["note"] = "idle（已释放显存）"
    if reader is not None:
        for attr in ("detector", "recognizer", "model"):
            try:
                setattr(reader, attr, None)
            except Exception:  # noqa: BLE001
                pass
        del reader
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    print("[ocr_server] model released, VRAM freed", flush=True)


def _idle_watchdog() -> None:
    """后台线程：空闲超过阈值就释放模型。"""
    while True:
        time.sleep(30)
        try:
            with _LOCK:
                idle = time.time() - _STATE["last_used"]
            if _STATE["ready"] and _STATE["last_used"] and idle > IDLE_RELEASE_SEC:
                release_model()
        except Exception:  # noqa: BLE001
            pass


def ocr_bytes(data: bytes):
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    with _LOCK:
        res = _STATE["reader"].readtext(img, detail=1)
    blocks = []
    for bbox, text, conf in res:
        blocks.append({"text": text, "conf": round(float(conf), 4),
                       "bbox": [[int(x), int(y)] for x, y in bbox]})
    return blocks


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": _STATE["ready"], "device": _STATE["device"],
                             "note": _STATE["note"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/ocr"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw or b"{}")
            # 懒加载：第一次识图才加载模型（10s+），加载失败则返回错误
            if not _STATE["ready"]:
                if not load_model():
                    self._send(200, {"ok": False,
                                     "error": "OCR not ready: " + _STATE["note"],
                                     "blocks": []})
                    return
            with _LOCK:
                _STATE["last_used"] = time.time()
            data = base64.b64decode(req.get("image_base64", ""))
            blocks = ocr_bytes(data)
            self._send(200, {"ok": True, "blocks": blocks, "count": len(blocks)})
        except Exception as e:  # noqa: BLE001
            self._send(200, {"ok": False, "error": str(e), "notes": [], "blocks": []})

    def log_message(self, *a):  # 静默访问日志
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 EasyOCR 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()
    # 启动时不加载模型（不占显存），第一次识图请求时才懒加载
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[ocr_server] listening on %s:%d (lazy-load, idle-release %.0fs)"
          % (args.host, args.port, IDLE_RELEASE_SEC), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
