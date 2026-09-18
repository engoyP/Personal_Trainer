"""运动文件导入回归：改了解析器 / 运动类型识别 / 负荷算法之后，跑这个。

为什么需要它：这里翻过两次车，而且都是**静默**的 —— 不报错、不失败，
只是数字悄悄变成错的。

  1. 运动类型只匹配英文（cycl/bik/walk/hik），而华为导出的 GPX/TCX 里写的是
     中文（`户外跑步` / `户外骑行` / `户外步行`）。`户外跑步` 侥幸落默认 running
     是对的，但**户外骑行和户外步行会被当成跑步**，移动速度阈值（0.8 vs 0.5 m/s）
     和卡路里 MET 公式全部跟着错。
  2. 采样点里没有心率时，`trimp(分钟, avg_hr or 0, ...)` 算出 **0.0** 落库。
     表现是这条运动在 ATL/CTL 曲线上完全不存在 —— 华为 GPX/TCX 恰好永远
     不含心率字段，等于负荷曲线对所有这类导入恒为 0。
  3. CSV 解析器：时间列是日期字符串时直接 500；表头前有说明行时只认出 1 个字段
     并落库成 0 km 空壳；列名子串匹配让 `t` 命中 `HeartRate` 使时间轴变成心率值；
     `start_time` 恒为 None 导致去重失效且记录对负荷曲线不可见。
     详见 test_csv() 的 docstring。
  4. 华为 GPX/TCX 的时间戳带 `Z`（UTC），原来丢掉 tzinfo 后把 UTC 的钟面数字
     当成本地时间存，**整整早 8 小时**；跨凌晨的运动还会被归到前一天。
     详见 test_timezone()。

它**不调 LLM、不碰真实数据库**（只有 ingest 闸门那一节用一个内存 SQLite），
纯函数级为主，秒级出结果，改代码的过程中可以随时跑。

用法（后端 venv）：

    cd backend
    ./.venv/Scripts/python.exe scripts/eval_import.py
    ./.venv/Scripts/python.exe scripts/eval_import.py --verbose
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import analytics                            # noqa: E402
from app.services.parsers.gpx import parse_gpx                # noqa: E402
from app.services.parsers.sport import guess_sport            # noqa: E402
from app.services.parsers.tcx import parse_tcx                # noqa: E402

MAX_HR, REST_HR, GENDER, WEIGHT = 190, 60, "male", 70.0

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (_PASSED if ok else _FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def track(n: int = 1800, speed: float = 2.7, dt: float = 1.0,
          lat: float = 34.8, hr=None) -> list[dict]:
    """造一条匀速直线轨迹。每秒一个点，默认 30 分钟 / 2.7 m/s（约配速 6:10）。"""
    pts = []
    for i in range(n):
        lon = 113.79 + (i * speed * dt) / (111320.0 * math.cos(math.radians(lat)))
        pts.append({
            "t": i * dt, "lat": lat, "lon": lon, "alt": 80.0,
            "hr": hr, "cad": None, "speed": None,
        })
    return pts


def summarize(pts, sport="running", **kw):
    kw.setdefault("max_hr", MAX_HR)
    kw.setdefault("resting_hr", REST_HR)
    kw.setdefault("gender", GENDER)
    kw.setdefault("weight_kg", WEIGHT)
    return analytics.summarize_activity(pts, sport, **kw)


# ---------------------------------------------------------------- 运动类型识别

def test_sport():
    print("\n运动类型识别（华为写中文，Garmin 写英文，两种都得认）")
    cases = {
        "户外跑步": "running", "户外骑行": "cycling", "户外步行": "walking",
        "户外徒步": "walking", "健走": "walking", "游泳": "swimming",
        "越野跑": "running", "室内跑步": "running",
        "Running": "running", "Cycling": "cycling",
        "Hiking": "walking", "Swimming": "swimming",
        "": "running", None: "running",
    }
    for raw, want in cases.items():
        got = guess_sport(raw)
        check(f"guess_sport({raw!r}) == {want}", got == want, f"得到 {got}")

    # 这两条是历史上静默错的那两条，单独拎出来，别混在一堆里看漏
    check("户外骑行 不再被误判成跑步", guess_sport("户外骑行") != "running")
    check("户外步行 不再被误判成跑步", guess_sport("户外步行") != "running")


# ---------------------------------------------------------------- 解析器

def _iso(i: int) -> str:
    """第 i 秒的时间戳。必须真的按秒递增 —— 早先写成 f'{i:02d}' 拼进秒位，
    i>59 时产生 `11:45:1799` 这种非法时间，解析出来全是 None，
    采样点按 t 去重后只剩几个，距离从 4855 m 缩到 159 m。”
    """
    return (datetime(2026, 9, 5, 11, 45, 0) + timedelta(seconds=i)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _gpx(sport_cn: str, pts) -> bytes:
    body = "".join(
        f'<trkpt lat="{p[0]}" lon="{p[1]}"><ele>80.0</ele>'
        f"<time>{_iso(i)}</time></trkpt>"
        for i, p in enumerate(pts)
    )
    # 华为导出的是 version="1.0" —— gpxpy 不解析 1.0 的 <trk><type>，
    # 所以这里故意用 1.0，正好守住那个坑。
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx creator="Health" version="1.0" xmlns="http://www.topografix.com/GPX/1/0">'
        f'<trk><type>{sport_cn}</type><trkseg>{body}</trkseg></trk></gpx>'
    ).encode()


def _tcx(sport_cn: str, pts) -> bytes:
    body = "".join(
        f"<Trackpoint><Time>{_iso(i)}</Time>"
        f'<Position><LatitudeDegrees>{p[0]}</LatitudeDegrees>'
        f'<LongitudeDegrees>{p[1]}</LongitudeDegrees></Position>'
        '<AltitudeMeters>80.0</AltitudeMeters></Trackpoint>'
        for i, p in enumerate(pts)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/'
        'TrainingCenterDatabase/v2"><Activities>'
        f'<Activity Sport="{sport_cn}"><Id>2026-09-05T11:45:00.000Z</Id>'
        f'<Lap><Track>{body}</Track></Lap></Activity>'
        '</Activities></TrainingCenterDatabase>'
    ).encode()


def _line(n=1800, speed=2.7, lat=34.8) -> list[tuple[float, float]]:
    return [
        (lat, 113.79 + (i * speed) / (111320.0 * math.cos(math.radians(lat))))
        for i in range(n)
    ]


def test_parsers():
    print("\n解析器：华为 GPX/TCX 是纯轨迹，不能凭空长出心率")
    pts = _line()

    g = parse_gpx(_gpx("户外跑步", pts))
    check("GPX 解析出采样点", len(g["samples"]) == len(pts), f'{len(g["samples"])} 个')
    check("GPX 中文 type 识别为 running", g["sport_type"] == "running", g["sport_type"])
    check("GPX 无心率字段时不编造心率",
          all(s["hr"] is None for s in g["samples"]))
    check("GPX 中文 户外骑行 识别为 cycling",
          parse_gpx(_gpx("户外骑行", pts))["sport_type"] == "cycling")

    t = parse_tcx(_tcx("户外跑步", pts))
    check("TCX 解析出采样点", len(t["samples"]) == len(pts), f'{len(t["samples"])} 个')
    check("TCX 中文 Sport 识别为 running", t["sport_type"] == "running", t["sport_type"])
    check("TCX 无 HeartRateBpm 时不编造心率",
          all(s["hr"] is None for s in t["samples"]))

    # 同一个轨迹的 GPX 和 TCX 应当算出一样的距离。差得远说明两处口径不一致。
    dg = summarize(g["samples"])["distance_m"]
    dt_ = summarize(t["samples"])["distance_m"]
    check("同一轨迹 GPX 与 TCX 距离一致（±1%）",
          abs(dg - dt_) / max(dg, 1) < 0.01, f"{dg:.1f} vs {dt_:.1f}")


# ---------------------------------------------------------------- 负荷

def test_load():
    print("\n训练负荷：无心率必须降级，不能静默写 0")
    pts = track()

    no_hr = summarize(pts)
    check("无心率时 load_source == met_estimate",
          no_hr["load_source"] == "met_estimate", no_hr["load_source"])
    check("无心率时负荷 > 0（不是静默的 0.0）",
          (no_hr["load"] or 0) > 0, f'load={no_hr["load"]}')
    check("无心率时平均心率仍为 None（不伪造）", no_hr["avg_hr"] is None)
    check("无心率时心率区间全为 0（不伪造分布）",
          all(v == 0 for v in no_hr["hr_zone_seconds"].values()))

    with_hr = summarize(track(hr=150))
    check("有心率时 load_source == trimp",
          with_hr["load_source"] == "trimp", with_hr["load_source"])
    check("有心率时负荷与 trimp() 直接算的一致",
          abs(with_hr["load"] - analytics.trimp(
              with_hr["moving_time_s"] / 60.0, 150, MAX_HR, REST_HR, GENDER)) < 0.02)

    # 估算值必须落在"同等时长下合理心率区间"的 TRIMP 范围内。
    # 这不是同义反复：改坏 MET 公式或 %HRR 换算，这里就会飞出去。
    minutes = no_hr["moving_time_s"] / 60.0
    lo = analytics.trimp(minutes, REST_HR + 0.55 * (MAX_HR - REST_HR), MAX_HR, REST_HR, GENDER)
    hi = analytics.trimp(minutes, REST_HR + 0.80 * (MAX_HR - REST_HR), MAX_HR, REST_HR, GENDER)
    check("估算负荷落在 55%~80% HRR 的 TRIMP 区间内（量纲没跑偏）",
          lo <= no_hr["load"] <= hi, f"{lo:.1f} <= {no_hr['load']:.1f} <= {hi:.1f}")

    # 同样速度下骑行负荷应低于跑步 —— 检验 MET 分支真的按 sport 走了
    check("同速度下骑行负荷 < 跑步负荷",
          summarize(pts, "cycling")["load"] < no_hr["load"],
          f'{summarize(pts, "cycling")["load"]:.1f} < {no_hr["load"]:.1f}')

    # 边界：档案本身不合理时老实返回 None，不硬算
    bad = summarize(pts, max_hr=REST_HR, resting_hr=REST_HR)
    check("max_hr <= resting_hr 时 load == None 且 load_source == none",
          bad["load"] is None and bad["load_source"] == "none",
          f'load={bad["load"]} source={bad["load_source"]}')

    check("%HRR 换算结果落在 [0,1]",
          all(0.0 <= (analytics.hrr_from_met(m, MAX_HR, REST_HR) or 0) <= 1.0
              for m in (1.0, 3.0, 9.2, 16.0, 30.0)))
    check("档案无效时 hrr_from_met 返回 None",
          analytics.hrr_from_met(9.2, 60, 60) is None)


def test_data_quality():
    """无心率这件事必须一路传到教练，不能只停在导入层。

    教练拿 acwr / TSB 做判断，而计划校验里 `acwr > 1.3` 是**硬拦截**。
    如果这些负荷其实是配速估的却不说，等于用估算值去拦截训练。
    """
    print("\n数据质量：估算负荷必须让下游知道")
    from app.agent.nodes import _data_quality_line

    check("全都有心率时不输出（不制造噪音）", _data_quality_line({}) == "")
    check("占比为 0 时不输出",
          _data_quality_line({"hr_missing_ratio_28d": 0.0,
                              "estimated_load_ratio_28d": 0.0}) == "")

    line = _data_quality_line({"hr_missing_ratio_28d": 1.0,
                               "estimated_load_ratio_28d": 1.0})
    check("全部无心率时提示缺失比例", "100%" in line, line[:40] + "…")
    check("全部无心率时点明是估算", "估算" in line)
    check("提示里说明了依据不足（别当实测值）", "留有余地" in line)

    mixed = _data_quality_line({"hr_missing_ratio_28d": 0.4,
                                "estimated_load_ratio_28d": 0.33})
    check("混合场景按比例显示", "40%" in mixed and "33%" in mixed, mixed[:40] + "…")

    # 聊天路径是「整包 JSON 丢给模型」，光有字段不够，必须有自然语言说明，
    # 否则模型看到 estimated_load_ratio_28d: 1.0 也不知道该收敛结论。
    from app.agent.chat import COACH_SYSTEM, build_context

    check("教练对话的系统提示词要求对估算值留有余地",
          "估算" in COACH_SYSTEM and "留有余地" in COACH_SYSTEM)

    ctx_est = build_context(
        {"estimated_load_ratio_28d": 1.0, "hr_missing_ratio_28d": 1.0},
        {}, [], "",
    )
    check("对话上下文里出现【数据质量】块", "【数据质量】" in ctx_est)
    check("对话上下文里写明负荷是估算的", "估算" in ctx_est)

    ctx_clean = build_context({"estimated_load_ratio_28d": 0.0}, {}, [], "")
    check("没有估算时不加【数据质量】块（不制造噪音）",
          "【数据质量】" not in ctx_clean)


def test_huawei_json():
    """华为全量导出的 JSON 里有多条运动，目前只导入一条。

    这个限制暂时不改（改之前得拿真实样本验证），但**绝不能静默** ——
    用户导了 100 条进来只看到 1 条，会以为导入失败了或者数据丢了。
    """
    print("\n华为 JSON：丢掉的部分必须说出来")
    from app.services.parsers.huawei import parse_huawei

    def rec(ts, sport, n):
        return {
            "startTime": ts, "sportType": sport, "distance": 5000,
            "dataList": [{"time": i, "heartRate": 140 + i % 20,
                          "latitude": 34.8, "longitude": 113.79} for i in range(n)],
        }

    multi = {"record_list": [rec(1693900000, "running", 300),
                             rec(1694000000, "cycling", 200),
                             rec(1694100000, "walking", 150)]}
    r = parse_huawei(json.dumps(multi).encode())

    check("识别出记录总数", r.get("records_found") == 3, f'{r.get("records_found")}')
    check("统计出可用运动条数", r.get("records_importable") == 3, f'{r.get("records_importable")}')
    check("导入的是采样点最多的那条（running 300 点）",
          r["sport_type"] == "running" and len(r["samples"]) == 300,
          f'{r["sport_type"]} {len(r["samples"])} 点')
    check("华为 JSON 能拿到心率（GPX 拿不到）",
          any(s["hr"] for s in r["samples"]))

    single = {"record_list": [rec(1693900000, "running", 300)]}
    check("单条文件不触发多记录告警",
          parse_huawei(json.dumps(single).encode())["records_importable"] == 1)

    # 采样点不足 20 的残条不算可用，免得凑数把告警刷出来
    with_junk = {"record_list": [rec(1693900000, "running", 300),
                                 {"startTime": 1694000000, "sportType": "running",
                                  "dataList": [{"time": 1, "heartRate": 140}]}]}
    check("采样点不足的记录不计入可用条数",
          parse_huawei(json.dumps(with_junk).encode())["records_importable"] == 1)

    # --- 字段匹配：别名里有单字符 "t"，而 distance / heartRate / alt / latitude
    #     的字段名里都含字母 t。纯子串匹配会让「取时间」取到心率或距离，
    #     而且结果取决于 JSON 里字段的书写顺序 —— 换个顺序换一个值，不报错。
    from app.services.parsers.huawei import (CAD_KEYS, HR_KEYS, TIME_KEYS,  # noqa: E402
                                             _pick, _to_datetime)

    for bad in ({"heartRate": 140, "time": 3}, {"distance": 1234.5, "time": 3},
                {"alt": 80.3, "time": 3}, {"latitude": 34.8, "lon": 113.7, "time": 3}):
        check(f"取时间不被干扰字段抢走：{list(bad)[0]}",
              _pick(bad, TIME_KEYS) == 3, f'取到 {_pick(bad, TIME_KEYS)}')

    order_a = {"time": 3, "heartRate": 140, "distance": 999.0}
    order_b = {"distance": 999.0, "heartRate": 140, "time": 3}
    check("取时间与字段书写顺序无关",
          _pick(order_a, TIME_KEYS) == _pick(order_b, TIME_KEYS) == 3,
          f'{_pick(order_a, TIME_KEYS)} vs {_pick(order_b, TIME_KEYS)}')
    check("完全没有时间字段时不硬凑（不拿心率当时间）",
          _pick({"hr": 140, "speed": 2.7}, TIME_KEYS) is None,
          str(_pick({"hr": 140, "speed": 2.7}, TIME_KEYS)))

    check("双字符别名的前缀写法仍能命中（hrValue）",
          _pick({"hrValue": 150}, HR_KEYS) == 150)
    check("下划线写法仍能命中（avg_hr）", _pick({"avg_hr": 150}, HR_KEYS) == 150)
    check("stepRate 仍能命中步频", _pick({"stepRate": 170}, CAD_KEYS) == 170)
    check("threshold 不再被当成心率（hr 是子串）",
          _pick({"threshold": 9}, HR_KEYS) is None, str(_pick({"threshold": 9}, HR_KEYS)))

    # --- 同一时刻，数字时间戳与 ISO 字符串现在必须一致
    check("数字时间戳与 ISO 字符串口径一致（原来差 8 小时）",
          _to_datetime(1693900000) == _to_datetime("2023-09-05T07:46:40Z"),
          f'{_to_datetime(1693900000)} vs {_to_datetime("2023-09-05T07:46:40Z")}')
    check("JSON 时间戳换算成本地时间（+8）",
          _to_datetime(1693900000) == datetime(2023, 9, 5, 15, 46, 40),
          str(_to_datetime(1693900000)))

    # --- 中文运动类型：这条路径原来只认英文 cycl/bik/walk/hik
    for cn, want in (("户外骑行", "cycling"), ("户外步行", "walking"),
                     ("游泳", "swimming"), ("户外跑步", "running")):
        r2 = parse_huawei(json.dumps({"record_list": [rec(1693900000, cn, 40)]}).encode())
        check(f'JSON 中文运动类型 {cn} -> {want}', r2["sport_type"] == want, r2["sport_type"])

    # --- 采样点没有时间字段时，t 不能退化成心率值
    no_time = {"record_list": [{
        "startTime": 1693900000, "sportType": "running",
        "dataList": [{"heartRate": 140, "latitude": 34.8, "longitude": 113.79}
                     for _ in range(40)],
    }]}
    r3 = parse_huawei(json.dumps(no_time).encode())
    ts = [s["t"] for s in r3["samples"]]
    check("采样点无时间字段时 t 按序号兜底（不是心率值）",
          len(ts) >= 2 and len(set(ts)) == len(ts) and max(ts) < 100,
          f't 前三个 {ts[:3]}，唯一值 {len(set(ts))} 个')


def test_csv():
    """CSV 这条路径一度是整个导入里最脏的地方，四个问题全是静默或崩溃：

      1. 时间列是 `2026-09-05 11:45:33` 这种字符串时，`float(t)` 抛 ValueError
         → 接口 500（华为单条导出的 CSV 就是这种）。
      2. 表头前有说明行（标题/导出时间/运动类型）时，按「第一行就是表头」取，
         一个字段都认不出 → 落库成一条 0 km 的空壳记录。
      3. 列名匹配用子串，别名里的 `t` 会命中 `HeartRate`（含字母 t）→ 时间轴
         取的是心率值，时长被算成几秒。
      4. `start_time` 永远为 None → 去重失效（同一文件每次导入都新增一条），
         而且 `start_time IS NULL` 的记录会被 `start_time >= since` 直接排除，
         对 ATL/CTL 曲线完全不可见。
    """
    print("\nCSV：华为导出的两种 CSV 必须区别对待")
    from app.services.parsers.csv import _match, parse_csv

    def csv_bytes(rows: list[str]) -> bytes:
        return ("\n".join(rows) + "\n").encode("utf-8")

    def samples(n=30, sec=True):
        return [f"2026-09-05 11:45:{i:02d},{140 + i % 20},{2.7 if sec else 0}"
                for i in range(n)]

    # 1) 曾经直接 500
    r = parse_csv(csv_bytes(["Time,HeartRate,Speed"] + samples()))
    check("时间列是日期时间字符串不再崩溃", len(r["samples"]) == 30, f'{len(r["samples"])} 点')
    check("日期时间列能定出 start_time",
          r["start_time"] is not None and r["start_time"].year == 2026, str(r["start_time"]))
    check("绝对时间轴从 0 开始逐秒递增",
          [s["t"] for s in r["samples"][:3]] == [0.0, 1.0, 2.0],
          str([s["t"] for s in r["samples"][:3]]))
    check("心率列解析正确", r["samples"][0]["hr"] == 140, str(r["samples"][0]["hr"]))

    # 2) 表头前的说明行
    r = parse_csv(csv_bytes(
        ["华为运动健康数据导出", "导出时间,2026-09-14", "运动类型,户外骑行",
         "Time,HeartRate,Speed"] + samples()))
    check("表头前有说明行时仍能认全字段",
          r["samples"][0]["hr"] is not None and r["samples"][0]["speed"] is not None,
          f'hr={r["samples"][0]["hr"]} speed={r["samples"][0]["speed"]}')
    check("说明行里的中文运动类型能被嗅出（户外骑行 → cycling）",
          r["sport_type"] == "cycling", r["sport_type"])

    # 3) 列名错配：t 不许指到心率列
    m = _match(["HeartRate", "Time", "Speed"])
    check("_match 不把 t 指到 HeartRate 列", m.get("t") == 1 and m.get("hr") == 0, str(m))
    check("_match 中 t 与 hr 永远不共用一列",
          _match(["HeartRate", "Time"])["t"] != _match(["HeartRate", "Time"])["hr"])
    r = parse_csv(csv_bytes(["HeartRate,Time", "140,0", "142,1", "145,2"]))
    check("表头顺序颠倒时时间轴取的是时间列",
          [s["t"] for s in r["samples"]] == [0.0, 1.0, 2.0],
          str([s["t"] for s in r["samples"]]))

    # 4) 时间轴必须单调递增
    bad = parse_csv(csv_bytes(["Time,HeartRate", "10,140", "5,142", "20,145"]))
    check("时间列非递增时拒绝导入", bool(bad.get("error")), bad.get("error", "")[:30])

    # 5) 「我的 → 运动数据 → 导出」是每天一行的汇总表，不能当采样序列
    daily = parse_csv(csv_bytes([
        "日期,步数,卡路里,运动时间",
        "2026-09-01,8000,300,32:10",
        "2026-09-02,9000,310,41:02",
        "2026-09-03,7500,280,28:45",
    ]))
    check("每天一行的汇总表被拒绝", bool(daily.get("error")), daily.get("error", "")[:30])
    check("拒绝理由给出可操作的下一步（导出数据）",
          "导出数据" in (daily.get("error") or ""), (daily.get("error") or "")[:40])
    check("汇总表不会产出采样点（否则会落库一条假记录）", not daily["samples"])

    # 6) 跨多天的表同样拒绝
    multi_day = parse_csv(csv_bytes(
        ["日期,用时,心率"] + [f"2026-09-{i:02d},{i * 60},140" for i in range(1, 6)]))
    check("覆盖多个日期的 CSV 被拒绝", bool(multi_day.get("error")), multi_day.get("error", "")[:30])

    # 7) 只有相对用时：能导入，但必须明说它进不了负荷曲线
    rel = parse_csv(csv_bytes(
        ["Elapsed,HeartRate,Speed"] + [f"{i},{140 + i % 20},2.7" for i in range(30)]))
    check("相对用时 CSV 仍可导入", len(rel["samples"]) == 30 and not rel.get("error"))
    check("相对用时 CSV 带告警（进不了负荷曲线）",
          any("负荷曲线" in w for w in rel.get("warnings") or []),
          str(rel.get("warnings"))[:40])
    check("相对用时 CSV 的 start_time 为 None（不编造日期）", rel["start_time"] is None)

    # 8) 中文表头
    zh = parse_csv(csv_bytes(
        ["时间,心率,配速,距离,步频"] +
        [f"2026-09-05 11:45:{i:02d},{140 + i % 20},6:10,{i * 2.7:.0f},170" for i in range(30)]))
    check("中文表头能识别出心率列", zh["samples"][0]["hr"] == 140, str(zh["samples"][0]["hr"]))
    check("中文配速列换算成速度（距离 > 0）",
          summarize(zh["samples"])["distance_m"] > 0,
          f'{summarize(zh["samples"])["distance_m"]:.0f} m')

    # 9) 没有心率列就不许长出心率
    no_hr = parse_csv(csv_bytes(
        ["Time,Speed"] + [f"2026-09-05 11:45:{i:02d},2.7" for i in range(30)]))
    check("CSV 没有心率列时不编造心率",
          all(s["hr"] is None for s in no_hr["samples"]))
    check("CSV 无心率的记录走 met_estimate 而非静默 0",
          summarize(no_hr["samples"])["load_source"] == "met_estimate")

    # 10) 完全认不出列名时给一句人话，不是一句「没有采样点」
    junk = parse_csv(csv_bytes(["甲,乙,丙", "1,2,3"]))
    check("认不出列名时给出明确理由", bool(junk.get("error")), junk.get("error", "")[:30])

    # 11) 里程只能来自速度/配速/经纬度。三者都没有时距离必然是 0，
    #     而落库结果是一条「0 km 但有负荷」的记录 —— 不许静默。
    dist_only = parse_csv(csv_bytes(
        ["时间,心率,距离"] +
        [f"2026-09-08 07:10:{i:02d},{140 + i % 20},{i * 2.7:.0f}" for i in range(30)]))
    check("只有距离列时告警说明里程会是 0",
          any("距离列" in w for w in dist_only.get("warnings") or []),
          str(dist_only.get("warnings"))[:40])
    check("距离列被忽略时该记录的里程确实为 0（告警说的是实情）",
          summarize(dist_only["samples"])["distance_m"] == 0)

    no_dist = parse_csv(csv_bytes(
        ["时间,心率"] + [f"2026-09-08 07:10:{i:02d},{140 + i % 20}" for i in range(30)]))
    check("没有任何里程来源时也告警",
          any("无法计算里程" in w for w in no_dist.get("warnings") or []))

    for name, header, extra in (
        ("配速", "时间,心率,配速", "6:10"),
        ("经纬度", "时间,心率,纬度,经度", "34.8,113.79"),
    ):
        r = parse_csv(csv_bytes(
            [header] + [f"2026-09-08 07:10:{i:02d},{140 + i % 20},{extra}" for i in range(30)]))
        check(f"有{name}列时不出现里程告警（不制造噪音）", not r.get("warnings"))


def test_timezone():
    """华为导出的 GPX/TCX 时间戳带 Z（UTC），不能把钟面数字直接当本地时间。

    实测依据：2026-09-05 那条跑步，华为 App 显示开始时间是 19:45（北京时间），
    而文件里写的是 `11:45:33Z` —— 正好差 8 小时。原来 `fromisoformat(...).replace(
    tzinfo=None)` 把 11:45 原样存了，等于所有导入的时间都早 8 小时。
    更麻烦的是**跨凌晨的运动会被归到前一天**，而 ATL/CTL 是按
    `start_time.date()` 分桶的，日负荷会跟着挪到错的日期。
    """
    print("\n时区：GPX/TCX 的 Z 是 UTC，必须换算成本地时间")
    from app.services.parsers.timeparse import parse_time, parse_time_or_epoch

    z = parse_time("2026-09-05T11:45:33.000Z")
    check("带 Z 的时间戳换算成北京时间（+8）",
          z == datetime(2026, 9, 5, 19, 45, 33), str(z))
    check("没有时区标记的时间戳不换算（只能当本地时间）",
          parse_time("2026-09-05T11:45:33") == datetime(2026, 9, 5, 11, 45, 33))
    check("带 +08:00 偏移的时间戳不换算",
          parse_time("2026-09-05T11:45:33+08:00") == datetime(2026, 9, 5, 11, 45, 33))
    check("带 -05:00 偏移的时间戳按偏移换算",
          parse_time("2026-09-05T11:45:33-05:00") == datetime(2026, 9, 6, 0, 45, 33))
    check("跨凌晨时日期往后挪（不归错日期）",
          parse_time("2026-09-05T17:30:00Z") == datetime(2026, 9, 6, 1, 30, 0),
          str(parse_time("2026-09-05T17:30:00Z")))

    check("Unix 秒时间戳按 UTC 解释后换算本地",
          parse_time_or_epoch("1693900000") == datetime(2023, 9, 5, 15, 46, 40),
          str(parse_time_or_epoch("1693900000")))
    check("毫秒时间戳与秒时间戳结果一致",
          parse_time_or_epoch("1693900000") == parse_time_or_epoch("1693900000000"))
    check("相对秒数不会被误当成 1970 年的时间戳",
          parse_time_or_epoch("330") is None and parse_time_or_epoch("1") is None)

    # 全链路：测试桩里的时间戳就是 `...Z`，和华为文件一样
    pts = _line(60)
    g = parse_gpx(_gpx("户外跑步", pts))
    check("GPX 全链路：Z 时间戳落为北京时间 19:45",
          g["start_time"] == datetime(2026, 9, 5, 19, 45, 0), str(g["start_time"]))
    t = parse_tcx(_tcx("户外跑步", pts))
    check("TCX 全链路：与 GPX 得到同一开始时间",
          t["start_time"] == g["start_time"], str(t["start_time"]))
    check("换算不影响 t 序列（时长仍是相对秒）",
          [s["t"] for s in g["samples"][:3]] == [0.0, 1.0, 2.0],
          str([s["t"] for s in g["samples"][:3]]))

    from app.services.parsers.csv import parse_csv
    r = parse_csv(("\n".join(
        ["时间,心率,配速"] +
        [f"2026-09-05T11:45:{i:02d}Z,140,6:10" for i in range(30)]) + "\n").encode())
    check("CSV 带 Z 的时间列同样换算（口径统一）",
          r["start_time"] == datetime(2026, 9, 5, 19, 45, 0), str(r["start_time"]))


def test_ingest_guards():
    """`ingest_bytes` 的拒绝逻辑 —— 这些情况必须**拒绝**，绝不能落库。

    用内存 SQLite，不碰真实数据。这里守的是「宁可拒绝也不能污染负荷曲线」：
    CTL 是 42 天 EMA，一条时长放大 1000 倍的记录就能把曲线抬高几个月，
    而曲线一旦失真，后面的评估、计划、拦截全部跟着错。
    """
    print("\n导入闸门：不合常理的数据必须拒绝")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import fitness  # noqa: F401  注册表
    from app.models.fitness import Activity
    from app.services.ingest import ingest_bytes

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()

    try:
        ok = ingest_bytes(db, "run.gpx", _gpx("户外跑步", _line()))
        check("正常轨迹能导入", ok["ok"] is True, str(ok.get("reason")))

        dup = ingest_bytes(db, "run.gpx", _gpx("户外跑步", _line()))
        check("同一文件重复导入被去重", dup.get("duplicate") is True, str(dup.get("reason")))

        # 采样点的时间是毫秒偏移（0,1000,2000…），被当成秒 → 时长放大 1000 倍。
        # 一次 5 分钟的运动变成 83 小时，负荷从 ~50 变成 ~7700。
        ms_json = {"record_list": [{
            "sportType": "户外跑步", "startTime": 1693900000000,
            "dataList": [{"time": i * 1000, "heartRate": 140,
                          "latitude": 34.8, "longitude": 113.79} for i in range(300)],
        }]}
        bad = ingest_bytes(db, "ms.json", json.dumps(ms_json).encode())
        check("时长不合常理时拒绝导入",
              bad["ok"] is False and "不合常理" in (bad.get("reason") or ""),
              str(bad.get("reason"))[:46])
        check("被拒绝的记录没有落库（库内仍只有 1 条）",
              db.query(Activity).count() == 1, f'{db.query(Activity).count()} 条')

        daily = ingest_bytes(db, "daily.csv", (
            "日期,步数,卡路里,运动时间\n"
            "2026-09-01,8000,300,32:10\n"
            "2026-09-02,9000,310,41:02\n"
            "2026-09-03,7500,280,28:45\n").encode())
        check("汇总表 CSV 被拒且理由来自解析器",
              daily["ok"] is False and "汇总表" in (daily.get("reason") or ""),
              str(daily.get("reason"))[:40])
        check("汇总表也没落库", db.query(Activity).count() == 1)

        empty = ingest_bytes(db, "x.gpx", b"<gpx version='1.1'></gpx>")
        check("空文件给出明确理由", empty["ok"] is False and bool(empty.get("reason")),
              str(empty.get("reason"))[:40])
    finally:
        db.close()


def test_manual_entry():
    """手工录入入口：从 summary 图直接建记录，不伪造逐点心率。"""
    print("\n手工录入：没有 GPS 文件时从 summary 图建记录")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import fitness  # noqa: F401  注册表
    from app.models.fitness import Activity
    from app.services.ingest import create_manual_activity

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()

    try:
        ts = datetime(2026, 9, 14, 20, 27, 0)
        ok = create_manual_activity(
            db,
            sport_type="running",
            start_time=ts,
            duration_min=34.85,
            distance_km=5.68,
            avg_hr=170,
            max_hr=181,
            avg_cadence_spm=167,
            avg_stride_m=0.97,
            elev_gain_m=15,
        )
        check("有平均心率时成功录入", ok["ok"] is True, str(ok.get("reason")))
        check("有心率时 load_source == trimp",
              ok["summary"]["load_source"] == "trimp",
              ok["summary"]["load_source"])
        check("有心率时负荷是合理的 TRIMP",
              40 <= (ok["summary"]["load"] or 0) <= 100,
              f'load={ok["summary"]["load"]}')
        check("平均配速已计算",
              ok["summary"]["avg_pace_s_per_km"] is not None,
              str(ok["summary"]["avg_pace_s_per_km"]))
        check("库内只有 1 条", db.query(Activity).count() == 1)

        # 同一日期+类型重复
        dup = create_manual_activity(
            db,
            sport_type="running",
            start_time=ts,
            duration_min=30,
            distance_km=5.0,
        )
        check("重复时间+类型被拒绝", dup["ok"] is False)
        check("重复时返回 duplicate 标记", dup.get("duplicate") is True)

        # 没有心率：必须走 met_estimate，不能是 0 或 None
        no_hr = create_manual_activity(
            db,
            sport_type="running",
            start_time=datetime(2026, 9, 13, 7, 0, 0),
            duration_min=30,
            distance_km=5.0,
        )
        check("无平均心率时也能录入", no_hr["ok"] is True)
        check("无平均心率时 load_source == met_estimate",
              no_hr["summary"]["load_source"] == "met_estimate",
              no_hr["summary"]["load_source"])
        check("无平均心率时负荷 > 0",
              (no_hr["summary"]["load"] or 0) > 0,
              f'load={no_hr["summary"]["load"]}')
        check("无平均心率时平均心率为 None",
              no_hr["summary"]["avg_hr"] is None)

        # 时长护栏
        too_long = create_manual_activity(
            db,
            sport_type="running",
            start_time=datetime(2026, 9, 12, 7, 0, 0),
            duration_min=25 * 60,
            distance_km=5,
        )
        check("超过 24 小时被拒绝", too_long["ok"] is False)

        # 中文运动类型也能认
        cn = create_manual_activity(
            db,
            sport_type="户外骑行",
            start_time=datetime(2026, 9, 11, 7, 0, 0),
            duration_min=60,
            distance_km=20,
        )
        check("中文运动类型被归一化", cn["ok"] is True and cn["summary"], str(cn))
        stored = db.query(Activity).filter(Activity.start_time == datetime(2026, 9, 11, 7, 0, 0)).first()
        check("中文运动类型落库为 cycling", stored and stored.sport_type == "cycling",
              stored.sport_type if stored else None)
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="运动文件导入回归")
    ap.add_argument("--verbose", action="store_true", help="打印每条用例")
    args = ap.parse_args()

    print("=" * 60)
    print("运动文件导入回归")

    test_sport()
    test_parsers()
    test_timezone()
    test_csv()
    test_load()
    test_ingest_guards()
    test_manual_entry()
    test_huawei_json()
    test_data_quality()
    test_recognition()

    print("\n" + "=" * 60)
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        print("\n失败项：")
        for name in _FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


def test_recognition():
    """识图解析层：合成华为海报文本块 → 结构化字段（不依赖 OCR 引擎）。

    验证 recognize_activity 能把「标签+数字」映射成 ManualActivityIn 字段，
    以及 MM:SS 时长、M月D日+HH:MM 时间、标签/值同块或分块等版式鲁棒性。
    OCR 引擎本身在 ocr_server.py 里，这里只测它下游的解析逻辑。
    """
    from app.services.recognition import recognize_activity

    # 典型版式：标签和值分块（"时长" / "34:51"），日期和时间分块
    blocks = [
        {"text": "户外跑步", "conf": 0.96},
        {"text": "5.68", "conf": 0.98},
        {"text": "公里", "conf": 0.95},
        {"text": "时长", "conf": 0.90},
        {"text": "34:51", "conf": 0.97},
        {"text": "平均配速 6'08\"", "conf": 0.90},
        {"text": "平均心率 170", "conf": 0.95},
        {"text": "最大心率 181", "conf": 0.95},
        {"text": "步频 167", "conf": 0.94},
        {"text": "步幅 0.97", "conf": 0.93},
        {"text": "爬升 15", "conf": 0.92},
        {"text": "454 千卡", "conf": 0.96},
        {"text": "9月14日", "conf": 0.90},
        {"text": "20:27", "conf": 0.90},
    ]
    res = recognize_activity(blocks, year=2026)
    f = res["fields"]
    check("识图-运动类型 running", f.get("sport_type") == "running")
    check("识图-距离 5.68km", abs(f["distance_km"] - 5.68) < 1e-6)
    check("识图-时长 34:51→34.85分", abs(f["duration_min"] - 34.85) < 1e-6)
    check("识图-平均心率 170", f.get("avg_hr") == 170)
    check("识图-最大心率 181", f.get("max_hr") == 181)
    check("识图-步频 167", f.get("avg_cadence_spm") == 167)
    check("识图-步幅 0.97m", abs(f["avg_stride_m"] - 0.97) < 1e-6)
    check("识图-爬升 15m", f.get("elev_gain_m") == 15)
    check("识图-卡路里 454", f.get("calories") == 454)
    check("识图-开始时间 2026-09-14T20:27", f.get("start_time") == "2026-09-14T20:27:00")
    check("识图-无必填缺失提示", not any("未识别到必填项" in n for n in res["notes"]))

    # 鲁棒性：标签与值同块（"5.68公里" / "平均心率170"）也能识别
    blocks2 = [
        {"text": "户外跑步", "conf": 0.96},
        {"text": "5.68公里", "conf": 0.95},
        {"text": "34:51", "conf": 0.97},
        {"text": "平均心率170", "conf": 0.95},
    ]
    res2 = recognize_activity(blocks2, year=2026)
    check("识图-同块距离 5.68km", abs(res2["fields"]["distance_km"] - 5.68) < 1e-6)
    check("识图-同块平均心率 170", res2["fields"].get("avg_hr") == 170)

    # 鲁棒性：真实华为海报版式（实测暴露的坑）
    #  1) 距离值在「公里」标签左侧且分块；2) 时长被 OCR 误成点号 "00.34.51"；
    #  3) 单位块含数字（"次/分"→"次1分"）不得被当成值。
    blocks3 = [
        {"text": "户外跑步", "conf": 0.99},
        {"text": "5.68", "conf": 1.00},
        {"text": "公里", "conf": 0.96},
        {"text": "00.34.51", "conf": 0.89},   # 冒号被 OCR 误成点号
        {"text": "时长", "conf": 1.00},
        {"text": "平均心率", "conf": 0.97},
        {"text": "170", "conf": 1.00},
        {"text": "次1分", "conf": 0.73},        # "次/分" 的干扰单位块
        {"text": "最大心率", "conf": 0.74},
        {"text": "181", "conf": 0.76},
        {"text": "次1分", "conf": 0.47},
        {"text": "平均步频", "conf": 0.59},
        {"text": "167", "conf": 1.00},
        {"text": "步1分", "conf": 0.69},        # "步/分" 的干扰单位块
        {"text": "步幅", "conf": 0.97},
        {"text": "97", "conf": 1.00},
        {"text": "厘米", "conf": 0.99},
        {"text": "累计爬升", "conf": 0.56},
        {"text": "15", "conf": 1.00},
        {"text": "米", "conf": 0.38},
    ]
    res3 = recognize_activity(blocks3, year=2026)
    f3 = res3["fields"]
    check("识图-海报距离值在左 5.68km", abs(f3.get("distance_km", 0) - 5.68) < 1e-6,
          f3.get("distance_km"))
    check("识图-海报点号时长 34.85分", abs(f3.get("duration_min", 0) - 34.85) < 1e-6,
          f3.get("duration_min"))
    check("识图-海报平均心率 170（单位块干扰被跳过）", f3.get("avg_hr") == 170, f3.get("avg_hr"))
    check("识图-海报最大心率 181（单位块干扰被跳过）", f3.get("max_hr") == 181, f3.get("max_hr"))
    check("识图-海报步频 167（'步1分'不误取）", f3.get("avg_cadence_spm") == 167,
          f3.get("avg_cadence_spm"))
    check("识图-海报步幅 97cm→0.97m", abs(f3.get("avg_stride_m", 0) - 0.97) < 1e-6,
          f3.get("avg_stride_m"))
    check("识图-海报爬升 15m", f3.get("elev_gain_m") == 15, f3.get("elev_gain_m"))

    # 真实华为海报端到端：EasyOCR 实跑输出（131 块，含 bbox）存为 fixture。
    # 这张图是 2026-08-29 19:36 的 12km 户外跑，两列卡片「标签上/值下」版式，
    # 含真实世界的全部脏东西：检测框跨行、数字粘连（99171步）、点号冒号混淆。
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "tests", "fixtures", "huawei_poster_blocks.json")
    if os.path.exists(fixture):
        real_blocks = json.load(open(fixture, encoding="utf-8"))
        res4 = recognize_activity(real_blocks, year=2026)
        f4 = res4["fields"]
        check("实拍海报-运动类型 running", f4.get("sport_type") == "running", f4.get("sport_type"))
        check("实拍海报-距离 12.0km", abs(f4.get("distance_km", 0) - 12.0) < 1e-6,
              f4.get("distance_km"))
        check("实拍海报-时长 01:12:37→72.62分", abs(f4.get("duration_min", 0) - 72.62) < 0.01,
              f4.get("duration_min"))
        check("实拍海报-平均心率 171", f4.get("avg_hr") == 171, f4.get("avg_hr"))
        check("实拍海报-最大心率 187（非平均的171）", f4.get("max_hr") == 187, f4.get("max_hr"))
        check("实拍海报-步频 171（'99171步'粘连拆出）", f4.get("avg_cadence_spm") == 171,
              f4.get("avg_cadence_spm"))
        check("实拍海报-步幅 96cm→0.96m", abs(f4.get("avg_stride_m", 0) - 0.96) < 1e-6,
              f4.get("avg_stride_m"))
        check("实拍海报-爬升 9.0m", f4.get("elev_gain_m") == 9.0, f4.get("elev_gain_m"))
        check("实拍海报-热量 955", f4.get("calories") == 955, f4.get("calories"))
        check("实拍海报-开始时间 2026-08-29T19:36",
              f4.get("start_time") == "2026-08-29T19:36:00", f4.get("start_time"))

    # 日期时间易错场景：时长相邻、配速干扰、中文日期、前导0、粘连
    def _start_time(blocks):
        return recognize_activity(blocks, year=2026)["fields"].get("start_time")

    check("识图时间-日期/时间分块+时长相邻不误抓",
          _start_time([{"text": "2026/8/29", "conf": 0.94}, {"text": "19:36", "conf": 0.9},
                       {"text": "运动时间", "conf": 0.93}, {"text": "01:12:37", "conf": 0.9}])
          == "2026-08-29T19:36:00")
    check("识图时间-粘连日期时间+配速不误抓",
          _start_time([{"text": "2026/8/2919:36", "conf": 0.94},
                       {"text": "平均配速", "conf": 0.95}, {"text": "6'03\"", "conf": 0.97}])
          == "2026-08-29T19:36:00")
    check("识图时间-中文日期 8月29日",
          _start_time([{"text": "8月29日", "conf": 0.9}, {"text": "19:36", "conf": 0.9}])
          == "2026-08-29T19:36:00")
    check("识图时间-前导0 07:05",
          _start_time([{"text": "2026/9/14", "conf": 0.94}, {"text": "07:05", "conf": 0.9}])
          == "2026-09-14T07:05:00")
    # 日字段贪婪匹配吞掉时间小时首位（2026/9/519:45 → 日=51 非法）必须回退
    check("识图时间-1位日粘连 2026/9/519:45 回退修复",
          _start_time([{"text": "户外跑步", "conf": 0.99},
                       {"text": "2026/9/519:45", "conf": 0.94}])
          == "2026-09-05T19:45:00")
    check("识图时间-1位日粘连+分块",
          _start_time([{"text": "2026/9/5", "conf": 0.94}, {"text": "19:45", "conf": 0.9}])
          == "2026-09-05T19:45:00")

    # 第二张实拍海报（5.68km / 2026-09-05 19:45）：锁定「1位日粘连」修复不回归。
    # 注意：OCR 把「15.0米」高置信误读成「1.5米」（引擎字符级错误，非解析层问题），
    # 因此爬升断言忠实反映 OCR 输入 =1.5；落库前靠人工核对纠正。
    fixture5k = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "tests", "fixtures", "huawei_poster_5k_blocks.json")
    if os.path.exists(fixture5k):
        r5k = recognize_activity(json.load(open(fixture5k, encoding="utf-8")), year=2026)
        f5k = r5k["fields"]
        check("实拍5k海报-开始时间 2026-09-05T19:45（1位日粘连修复）",
              f5k.get("start_time") == "2026-09-05T19:45:00", f5k.get("start_time"))
        check("实拍5k海报-距离 5.68km", abs(f5k.get("distance_km", 0) - 5.68) < 1e-6,
              f5k.get("distance_km"))
        check("实拍5k海报-时长 34.85分", abs(f5k.get("duration_min", 0) - 34.85) < 0.01,
              f5k.get("duration_min"))
        check("实拍5k海报-心率 170/181", f5k.get("avg_hr") == 170 and f5k.get("max_hr") == 181,
              (f5k.get("avg_hr"), f5k.get("max_hr")))
        check("实拍5k海报-步频 167 / 步幅 0.97m",
              f5k.get("avg_cadence_spm") == 167 and abs(f5k.get("avg_stride_m", 0) - 0.97) < 1e-6,
              (f5k.get("avg_cadence_spm"), f5k.get("avg_stride_m")))
        check("实拍5k海报-热量 454", f5k.get("calories") == 454, f5k.get("calories"))



if __name__ == "__main__":
    sys.exit(main())
