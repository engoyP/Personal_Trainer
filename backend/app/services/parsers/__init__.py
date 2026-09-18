from __future__ import annotations

import json

from .gpx import parse_gpx
from .tcx import parse_tcx
from .csv import parse_csv
from .huawei import parse_huawei, probe as probe_json


def parse_file(filename: str, content: bytes) -> dict:
    name = (filename or "").lower()
    if name.endswith(".gpx"):
        return parse_gpx(content)
    if name.endswith(".tcx"):
        return parse_tcx(content)
    if name.endswith(".csv"):
        return parse_csv(content)
    if name.endswith(".json"):
        return parse_huawei(content)
    raise ValueError(f"不支持的文件类型：{filename}（仅支持 .gpx / .tcx / .csv / .json）")


def probe_file(filename: str, content: bytes) -> dict:
    """只探测结构不写入，用于导入前确认。"""
    name = (filename or "").lower()
    if name.endswith(".json"):
        return probe_json(json.loads(content.decode("utf-8-sig", errors="ignore")))

    parsed = parse_file(filename, content)
    samples = parsed.get("samples") or []
    keys = sorted({k for s in samples[:5] for k, v in s.items() if v is not None})
    return {
        "top_keys": [],
        "is_list": False,
        "candidates": [{"path": "$", "count": len(samples), "keys": keys}],
        "sport_type": parsed.get("sport_type"),
        "start_time": str(parsed.get("start_time")),
        "error": parsed.get("error"),
        "warnings": parsed.get("warnings") or [],
    }
