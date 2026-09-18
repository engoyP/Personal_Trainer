from __future__ import annotations

import gpxpy
from xml.etree import ElementTree as ET

from .sport import guess_sport
from .timeparse import to_local_naive


def _type_from_xml(text: str) -> str | None:
    """自己从原始 XML 里捞 <trk><type>。

    gpxpy **只解析 GPX 1.1 的 track type**，1.0 的一律返回 None ——
    而华为运动健康导出的 GPX 恰好是 `version="1.0"`，运动类型就写在
    `<trk><type>户外跑步</type>` 里。不自己捞的话，中文类型永远拿不到，
    户外骑行 / 户外步行会全部退化成默认 running（速度阈值和卡路里公式跟着错）。
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if str(el.tag).split("}")[-1].lower() == "type" and (el.text or "").strip():
            return el.text.strip()
    return None


def _text_to_float(text):
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return None


def _ext_value(point, keys: list[str]):
    """从 GPX extensions 里模糊查找数值。

    gpxpy 返回的 extensions 可能是 lxml 元素、嵌套 dict 或 list，
    三种结构都要覆盖，不同厂商写法差异很大。
    """
    exts = getattr(point, "extensions", None) or []

    def walk(node):
        # lxml / ElementTree 元素
        if hasattr(node, "tag") and hasattr(node, "iter"):
            for el in node.iter():
                tag = str(el.tag).lower()
                if any(k in tag for k in keys):
                    v = _text_to_float(el.text)
                    if v is not None:
                        return v
            return None

        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if any(kk in lk for kk in keys):
                    if isinstance(v, (int, float)):
                        return float(v)
                    found = _text_to_float(v)
                    if found is not None:
                        return found
                found = walk(v)
                if found is not None:
                    return found
            return None

        if isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found

        return None

    return walk(exts)


def parse_gpx(content: bytes) -> dict:
    text = content.decode("utf-8", errors="ignore")
    gpx = gpxpy.parse(text)

    raw_points: list[dict] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                hr = _ext_value(p, ["hr", "heartrate", "heart_rate", "heart"])
                cad = _ext_value(p, ["cad", "cadence", "step"])
                spd = _ext_value(p, ["speed"])
                raw_points.append(
                    {
                        # gpxpy 对带 Z 的时间返回 aware datetime（SimpleTZ('Z')），
                        # 必须换算到本地再存，否则 UTC 的钟面数字会被当成本地时间。
                        "time": to_local_naive(p.time),
                        "lat": p.latitude,
                        "lon": p.longitude,
                        "alt": p.elevation,
                        "hr": int(hr) if hr else None,
                        "cad": cad,
                        "speed": spd,
                    }
                )

    if not raw_points:
        return {"sport_type": "unknown", "start_time": None,
                "external_id": None, "samples": []}

    start = next((p["time"] for p in raw_points if p["time"]), None)
    samples = []
    for p in raw_points:
        t = (p["time"] - start).total_seconds() if (p["time"] and start) else None
        samples.append(
            {
                "t": t,
                "lat": p["lat"],
                "lon": p["lon"],
                "alt": p["alt"],
                "hr": p["hr"],
                "cad": p["cad"],
                "speed": p["speed"],
            }
        )

    # 时间戳缺失时用索引兜底，保证 t 单调
    if start is None:
        for i, s in enumerate(samples):
            s["t"] = float(i)

    raw_type = gpx.tracks[0].type if gpx.tracks else None
    if not raw_type:
        raw_type = _type_from_xml(text)
    sport = guess_sport(raw_type)

    return {
        "sport_type": sport,
        "start_time": start,
        "external_id": str(start) if start else None,
        "samples": samples,
    }
