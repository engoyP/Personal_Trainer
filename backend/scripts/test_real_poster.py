"""Real-poster OCR accuracy probe.

Runs EasyOCR directly on one or more Huawei share-poster images, then feeds the
recovered text blocks into the engine-agnostic parsing layer
(`app.services.recognition.recognize_activity`) and prints, per image:

  - every raw OCR block (text / conf / bbox) so you can see what OCR actually got
  - the structured fields the parser extracted
  - the parser's notes (missing required fields etc.)

This is the "can OCR handle this task?" empirical answer — no guessing.
Run from backend/ with the isolated ocr_venv (which has easyocr installed):

    ..\ocr_venv\Scripts\python.exe scripts/test_real_poster.py "<image.jpg>" [more images...]

If no image path is given it falls back to the user's known poster path.
"""
from __future__ import annotations

import sys
import os

# make `app` importable when run from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import easyocr  # type: ignore
from app.services.recognition import recognize_activity

DEFAULT_IMAGE = (
    r"D:\电脑管家迁移文件\xwechat_files\wxid_av3u0uddd3l922_f246\temp\RWTemp"
    r"\2026-09\9e20f478899dc29eb19741386f9343c8"
    r"\6e8c6c976626a7c89541be0ab7fe091b.jpg"
)


def ocr_blocks(reader: "easyocr.Reader", path: str):
    import cv2  # type: ignore
    img = cv2.imdecode(np_from_file(path), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot decode image: {path}")
    raw = reader.readtext(img, detail=1)
    blocks = []
    for bbox, text, conf in raw:
        blocks.append({"text": text.strip(), "conf": round(float(conf), 4), "bbox": bbox})
    return blocks


def np_from_file(path: str):
    import numpy as np  # type: ignore
    with open(path, "rb") as f:
        return np.frombuffer(f.read(), dtype=np.uint8)


def main() -> None:
    paths = sys.argv[1:] or [DEFAULT_IMAGE]
    use_gpu = os.environ.get("OCR_CUDA", "0") != "0"
    model_dir = os.environ.get("EASYOCR_MODEL_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "easyocr_models"
    )
    os.makedirs(model_dir, exist_ok=True)
    print(f"[easyocr] loading model (ch_sim+en), gpu={use_gpu}, dir={model_dir}…")
    reader = easyocr.Reader(
        ["ch_sim", "en"], gpu=use_gpu, model_storage_directory=model_dir
    )

    for path in paths:
        if not os.path.exists(path):
            print(f"\n=== SKIP (missing): {path} ===")
            continue
        print(f"\n=== IMAGE: {path} ===")
        try:
            blocks = ocr_blocks(reader, path)
        except Exception as e:  # noqa: BLE001
            print(f"  OCR error: {e!r}")
            continue

        print(f"\n--- raw OCR blocks ({len(blocks)}) ---")
        for i, b in enumerate(blocks):
            flag = "" if b["conf"] >= 0.5 else "  <-- low conf"
            print(f"  [{i:02d}] conf={b['conf']:.2f} {b['text']!r}{flag}")

        result = recognize_activity(blocks, year=2026)
        print("\n--- parsed fields ---")
        for k, v in result["fields"].items():
            print(f"  {k}: {v!r}")
        print(f"  confidence: {result.get('confidence')}")
        if result.get("notes"):
            print("  notes:")
            for n in result["notes"]:
                print(f"    - {n}")
        print(f"  block_count: {result.get('block_count')}")


if __name__ == "__main__":
    main()
