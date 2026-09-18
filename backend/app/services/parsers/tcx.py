from __future__ import annotations

from xml.etree import ElementTree as ET

from .sport import guess_sport
from .timeparse import parse_time

NS = {
    "tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
    "ext": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
}


def _f(node) -> float | None:
    if node is None or node.text is None:
        return None
    try:
        return float(node.text)
    except ValueError:
        return None


def parse_tcx(content: bytes) -> dict:
    root = ET.fromstring(content.decode("utf-8", errors="ignore"))

    activity = root.find(".//tcx:Activity", NS)
    sport = guess_sport(activity.get("Sport") if activity is not None else None)

    raw_points = []
    for tp in root.findall(".//tcx:Trackpoint", NS):
        time = parse_time(tp.findtext("tcx:Time", namespaces=NS))
        lat = _f(tp.find("tcx:Position/tcx:LatitudeDegrees", NS))
        lon = _f(tp.find("tcx:Position/tcx:LongitudeDegrees", NS))
        alt = _f(tp.find("tcx:AltitudeMeters", NS))
        hr_node = tp.find("tcx:HeartRateBpm/tcx:Value", NS)
        hr = _f(hr_node)
        cad = _f(tp.find("tcx:Cadence", NS))
        speed = _f(tp.find("tcx:Extensions/ext:TPX/ext:Speed", NS))
        raw_points.append(
            {
                "time": time,
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "hr": int(hr) if hr else None,
                "cad": cad,
                "speed": speed,
            }
        )

    if not raw_points:
        return {"sport_type": sport, "start_time": None,
                "external_id": None, "samples": []}

    # 跑步的 Cadence 在 TCX 里是单脚步数，需要乘 2 得到常规步频（步/分钟）
    if sport in ("running", "walking"):
        vals = [p["cad"] for p in raw_points if p["cad"]]
        if vals and max(vals) < 130:
            for p in raw_points:
                if p["cad"]:
                    p["cad"] = p["cad"] * 2

    start = next((p["time"] for p in raw_points if p["time"]), None)
    samples = []
    for p in raw_points:
        t = (p["time"] - start).total_seconds() if (p["time"] and start) else None
        samples.append({**{k: v for k, v in p.items() if k != "time"}, "t": t})

    if start is None:
        for i, s in enumerate(samples):
            s["t"] = float(i)

    return {
        "sport_type": sport,
        "start_time": start,
        "external_id": str(start) if start else None,
        "samples": samples,
    }
