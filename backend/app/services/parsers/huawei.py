from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .sport import guess_sport
from .timeparse import parse_time_or_epoch

TIME_KEYS = ["starttime", "start_time", "begintime", "timestamp", "time", "date"]
DIST_KEYS = ["distance", "totaldistance", "dist"]
DUR_KEYS = ["duration", "totaltime", "costtime", "elapsed"]
SPORT_KEYS = ["sporttype", "sport", "motiontype", "type"]
HR_KEYS = ["heartrate", "hr", "avghr", "avg_hr", "heart"]
CAD_KEYS = ["steprate", "cadence", "cad", "stepfrequency", "frequency"]
SAMPLE_LIST_KEYS = ["datalist", "pointlist", "points", "samples", "details",
                    "trajectory", "track", "records", "list"]

# 少于这个采样点算不出任何指标（analytics 里的下限），不算一条可用记录
MIN_SAMPLES = 20
# 一次最多扫多少条记录，防止全量导出（几百上千条）拖死
SCAN_LIMIT = 200


def _lk(s: str) -> str:
    return str(s).lower().replace("_", "").replace(" ", "")


def _pick(d: dict, keys: list[str]):
    """按字段名取第一个命中的值。**全等优先，短别名只认前缀。**

    别名里有单字符 `"t"`，而 `distance` / `heartRate` / `alt` / `latitude`
    这些字段名里都含字母 t。纯子串匹配会让「取时间」直接取到心率或距离 ——
    更糟的是结果**取决于 JSON 里字段的书写顺序**：`{"time":3,"heartRate":140}`
    取到 3，调换顺序就取到 140。这种错不报错，只是数字悄悄全变。

    规则：全等 > 单字符别名只认全等 > 双字符别名认前缀 > 长别名才允许子串。
    """
    norm_keys = [_lk(k) for k in keys]
    items = [(_lk(k), v) for k, v in d.items()]

    for nk, v in items:                              # 1) 全等
        if nk in norm_keys:
            return v

    medium = [k for k in norm_keys if len(k) == 2]    # 2) 双字符认前缀（hrValue）
    for nk, v in items:
        if any(nk.startswith(k) for k in medium):
            return v

    long_keys = [k for k in norm_keys if len(k) >= 3]  # 3) 长别名允许子串
    for nk, v in items:
        if any(k in nk for k in long_keys):
            return v
    return None


def _to_datetime(v: Any):
    """统一走 timeparse，和 GPX / TCX / CSV 保持同一套时区口径。

    这里原来是数字走 `datetime.fromtimestamp`（隐含按宿主时区换算）、
    字符串却 `.replace(tzinfo=None)`（把 UTC 的钟面数字当本地时间），
    **同一个时刻两条分支差 8 小时**。
    """
    return parse_time_or_epoch(v)


def _to_float(v: Any):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _find_dict_lists(node: Any, path: str = "$", depth: int = 0, out: list | None = None):
    """递归收集所有「元素是字典」的列表，作为运动记录候选。"""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.append({"path": f"{path}.{k}", "items": v})
            else:
                _find_dict_lists(v, f"{path}.{k}", depth + 1, out)
    elif isinstance(node, list):
        for i, v in enumerate(node[:50]):
            _find_dict_lists(v, f"{path}[{i}]", depth + 1, out)
    return out


def probe(data: Any) -> dict:
    """结构探测：不导入，只告诉前端这个文件里识别到了什么。"""
    candidates = _find_dict_lists(data)
    summary = []
    for c in candidates[:12]:
        keys = list(c["items"][0].keys())
        summary.append({"path": c["path"], "count": len(c["items"]), "keys": keys[:20]})
    return {
        "top_keys": list(data.keys())[:30] if isinstance(data, dict) else [],
        "is_list": isinstance(data, list),
        "candidates": summary,
    }


def _extract_samples(record: dict, start: datetime | None):
    """在一条记录里找采样点序列。"""
    best = None
    for k, v in record.items():
        if not (isinstance(v, list) and v and isinstance(v[0], dict)):
            continue
        if not any(key in _lk(k) for key in SAMPLE_LIST_KEYS):
            continue
        if best is None or len(v) > len(best):
            best = v

    if best is None:
        # 兜底：任意含心率或时间字段的子列表
        for v in record.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                keys = {_lk(k) for k in v[0].keys()}
                if keys & {"heartrate", "hr", "heart", "time", "timestamp"}:
                    if best is None or len(v) > len(best):
                        best = v

    if not best:
        return []

    samples = []
    for p in best:
        t_raw = _pick(p, ["time", "t", "timestamp", "second", "elapsed"])
        t = _to_float(t_raw)
        if t is None:
            dt = _to_datetime(t_raw)
            t = (dt - start).total_seconds() if (dt and start) else None
        if t is None:
            t = float(len(samples))
        if t > 1e11:
            t = t / 1000.0
            if start:
                t = t - start.timestamp()

        hr = _to_float(_pick(p, HR_KEYS))
        cad = _to_float(_pick(p, CAD_KEYS))
        speed = _to_float(_pick(p, ["speed", "velocity", "pacespeed"]))
        samples.append(
            {
                "t": t,
                "lat": _to_float(_pick(p, ["latitude", "lat"])),
                "lon": _to_float(_pick(p, ["longitude", "lon", "lng"])),
                "alt": _to_float(_pick(p, ["altitude", "alt", "elevation"])),
                "hr": int(hr) if hr else None,
                "cad": cad,
                "speed": speed,
            }
        )
    return samples


def parse_huawei(content: bytes) -> dict:
    data = json.loads(content.decode("utf-8-sig", errors="ignore"))

    candidates = _find_dict_lists(data)
    record_list = None
    if candidates:
        record_list = max(candidates, key=lambda c: len(c["items"]))["items"]

    if record_list is None:
        record_list = [data] if isinstance(data, dict) else []

    # 目前按「一个文件 = 一次运动」处理：取采样点最多的那条记录。
    # 剩下的会被丢掉 —— 这不是 bug 但是**静默**的，所以顺便数清楚有多少条可用，
    # 交给上层把话说出来，别让用户以为全导进去了。
    best_record = None
    best_samples: list = []
    importable = 0
    for rec in record_list[:200]:
        start = _to_datetime(_pick(rec, TIME_KEYS))
        s = _extract_samples(rec, start)
        if len(s) >= MIN_SAMPLES:      # 和 analytics 能算出指标的下限一致
            importable += 1
        if len(s) > len(best_samples):
            best_samples = s
            best_record = (rec, start)

    if best_record is None:
        return {"sport_type": "unknown", "start_time": None,
                "external_id": None, "samples": [],
                "records_found": len(record_list), "records_importable": 0,
                "records_truncated": len(record_list) > SCAN_LIMIT}

    meta = {
        "records_found": len(record_list),
        "records_importable": importable,
        "records_truncated": len(record_list) > SCAN_LIMIT,
    }

    rec, start = best_record
    # 必须走 guess_sport：这里原来只认英文的 cycl/bik/walk/hik，
    # 华为写的「户外骑行」「户外步行」「游泳」会全部退化成 running
    # —— 和 GPX / TCX 早期那个 bug 一模一样，只是这条路径漏改了。
    sport = guess_sport(_pick(rec, SPORT_KEYS))

    return {
        "sport_type": sport,
        "start_time": start,
        "external_id": str(start) if start else None,
        "samples": best_samples,
        "raw_summary": {
            "distance": _to_float(_pick(rec, DIST_KEYS)),
            "duration": _to_float(_pick(rec, DUR_KEYS)),
            "avg_hr": _to_float(_pick(rec, HR_KEYS)),
        },
        **meta,
    }
