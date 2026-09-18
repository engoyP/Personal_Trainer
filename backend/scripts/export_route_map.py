"""把跑过的轨迹导出成一份独立 HTML 地图，可直接在浏览器打开。

坐标在前端做 WGS-84 → GCJ-02 转换后再叠加到腾讯地图上。
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
OUT = Path("D:/WorkBuddy/outputs/route-map.html")

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>我的跑步路线</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: system-ui, -apple-system, 'Microsoft YaHei', sans-serif; }
  #map { width: 100%; height: 100vh; }
  .panel {
    position: absolute; top: 16px; left: 16px; z-index: 1000;
    background: rgba(255,255,255,.96); border: 1px solid #e4e6eb;
    border-radius: 12px; padding: 14px 16px; max-width: 260px;
    box-shadow: 0 2px 12px rgba(0,0,0,.06);
  }
  .panel h1 { margin: 0 0 4px; font-size: 15px; font-weight: 600; color: #0d9488; }
  .panel .sub { font-size: 12px; color: #6b7280; margin-bottom: 10px; }
  .panel ul { margin: 0; padding-left: 0; list-style: none; font-size: 12px; }
  .panel li { display: flex; align-items: center; gap: 8px; padding: 3px 0; color: #1f2328; }
  .dot { width: 10px; height: 10px; border-radius: 3px; flex: none; }
  .empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #6b7280; }
</style>
<script type="text/javascript">
  window._TMapSecurityConfig = {
    serviceHost: 'http://127.0.0.1:__WB_HTTP_PORT__/_TMapService/_wbt/__WB_TMAP_SECRET__',
  };
</script>
<script src="https://map.qq.com/api/gljs?v=1.exp"></script>
</head>
<body>
<div id="map"></div>
<div class="panel" id="panel"></div>
<script>
const TRACKS = __DATA__;
const COLORS = ['#0d9488','#2563eb','#f59e0b','#dc2626','#8b5cf6','#0891b2'];
const PI = Math.PI, A = 6378245.0, EE = 0.00669342162296594323;

function outOfChina(lat, lon) { return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271; }
function tLat(x, y) {
  let r = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
  r += (20.0*Math.sin(6.0*x*PI) + 20.0*Math.sin(2.0*x*PI)) * 2.0/3.0;
  r += (20.0*Math.sin(y*PI) + 40.0*Math.sin(y/3.0*PI)) * 2.0/3.0;
  r += (160.0*Math.sin(y/12.0*PI) + 320*Math.sin(y*PI/30.0)) * 2.0/3.0;
  return r;
}
function tLon(x, y) {
  let r = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
  r += (20.0*Math.sin(6.0*x*PI) + 20.0*Math.sin(2.0*x*PI)) * 2.0/3.0;
  r += (20.0*Math.sin(x*PI) + 40.0*Math.sin(x/3.0*PI)) * 2.0/3.0;
  r += (150.0*Math.sin(x/12.0*PI) + 300.0*Math.sin(x/30.0*PI)) * 2.0/3.0;
  return r;
}
function toGcj(lat, lon) {
  if (outOfChina(lat, lon)) return [lat, lon];
  let dLat = tLat(lon - 105.0, lat - 35.0), dLon = tLon(lon - 105.0, lat - 35.0);
  const rad = lat / 180.0 * PI;
  let magic = Math.sin(rad); magic = 1 - EE * magic * magic;
  const sq = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sq) * PI);
  dLon = (dLon * 180.0) / (A / sq * Math.cos(rad) * PI);
  return [lat + dLat, lon + dLon];
}

const panel = document.getElementById('panel');
if (!TRACKS.length) {
  panel.innerHTML = '<div class="empty">还没有带轨迹的运动记录</div>';
} else {
  const totalKm = TRACKS.reduce((s, t) => s + t.distance_km, 0);
  panel.innerHTML = '<h1>我的跑步路线</h1>' +
    '<div class="sub">' + TRACKS.length + ' 条 · 共 ' + totalKm.toFixed(2) + ' km</div>' +
    '<ul>' + TRACKS.map((t, i) =>
      '<li><span class="dot" style="background:' + COLORS[i % COLORS.length] + '"></span>' +
      t.date + ' · ' + t.distance_km + ' km</li>').join('') + '</ul>';

  const gcj = TRACKS.map(t => t.points.map(p => toGcj(p[0], p[1])));
  const all = gcj.flat();
  const map = new TMap.Map('map', {
    zoom: 13,
    center: new TMap.LatLng(all[0][0], all[0][1]),
  });

  new TMap.MultiPolyline({
    map,
    styles: Object.fromEntries(gcj.map((_, i) => ['s' + i, new TMap.PolylineStyle({
      color: COLORS[i % COLORS.length], width: 5, borderWidth: 1, borderColor: '#ffffff', lineCap: 'round',
    })])),
    geometries: gcj.map((pts, i) => ({
      id: 't' + i, styleId: 's' + i,
      paths: pts.map(p => new TMap.LatLng(p[0], p[1])),
    })),
  });

  const bounds = new TMap.LatLngBounds();
  all.forEach(p => bounds.extend(new TMap.LatLng(p[0], p[1])));
  map.fitBounds(bounds, { padding: 60 });
}
</script>
</body>
</html>
"""


def main() -> None:
    data = requests.get(f"{BASE}/api/activities/tracks?limit=30", timeout=30).json()
    tracks = [
        {"date": t["date"], "distance_km": t["distance_km"], "points": t["points"]}
        for t in data["items"]
    ]

    html = TEMPLATE.replace("__DATA__", json.dumps(tracks, ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    total = sum(t["distance_km"] for t in tracks)
    print(f"已生成: {OUT}")
    print(f"轨迹 {len(tracks)} 条，合计 {total:.2f} km")


if __name__ == "__main__":
    main()
