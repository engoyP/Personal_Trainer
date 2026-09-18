"""生成带心率与步频扩展的示例 GPX，用于验证导入与分析链路。"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "uploads"
OUT.mkdir(parents=True, exist_ok=True)

random.seed(7)


def make_gpx(start: datetime, minutes: float, base_hr: int,
             base_cad: int, dist_km: float, name: str) -> None:
    step_s = 5
    n = max(int(minutes * 60 / step_s), 10)
    lat0, lon0 = 34.7520, 113.6250
    total_m = dist_km * 1000.0

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="sample"'
        ' xmlns="http://www.topografix.com/GPX/1/1"'
        ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        "<trk><type>Running</type><trkseg>",
    ]

    for i in range(n):
        frac = i / (n - 1)
        t = start + timedelta(seconds=i * step_s)
        d = total_m * frac
        la = lat0 + (d / 111000.0) * 0.7
        lo = lon0 + (d / 111000.0) * 0.7
        hr = int(base_hr + 25 * frac + random.uniform(-3, 3))
        cad = int(base_cad + random.uniform(-3, 3))
        ele = 100 + 40 * math.sin(frac * math.pi * 3)
        lines.append(
            f'<trkpt lat="{la:.6f}" lon="{lo:.6f}">'
            f"<ele>{ele:.1f}</ele>"
            f'<time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
            "<extensions><gpxtpx:TrackPointExtension>"
            f"<gpxtpx:hr>{hr}</gpxtpx:hr>"
            f"<gpxtpx:cad>{cad}</gpxtpx:cad>"
            "</gpxtpx:TrackPointExtension></extensions>"
            "</trkpt>"
        )

    lines.append("</trkseg></trk></gpx>")
    (OUT / name).write_text("\n".join(lines), encoding="utf-8")
    print("generated", name)


if __name__ == "__main__":
    today = datetime.now().replace(hour=6, minute=0, second=0, microsecond=0)
    plan = [
        (21, 32, 138, 168, 5.2),
        (17, 45, 142, 170, 7.0),
        (12, 28, 145, 172, 4.5),
        (8, 62, 140, 166, 10.0),
        (4, 25, 150, 174, 4.0),
        (1, 38, 144, 171, 6.0),
    ]
    for days_ago, mins, hr, cad, km in plan:
        start = today - timedelta(days=days_ago)
        make_gpx(start, mins, hr, cad, km, f"sample_run_{start.strftime('%Y%m%d')}.gpx")
