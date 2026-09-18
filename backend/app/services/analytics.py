from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from app.config import settings

# 心率区间，按最大心率百分比划分
HR_ZONES = [
    ("z1", 0.50, 0.60),
    ("z2", 0.60, 0.70),
    ("z3", 0.70, 0.80),
    ("z4", 0.80, 0.90),
    ("z5", 0.90, 1.01),
]

# 单次运动的时长上限（秒）。超过它一定是解析出了问题，而不是真有人连续运动 24 小时。
# 最常见的成因是时间字段单位不是秒（毫秒会被放大 1000 倍）——一次 30 分钟的运动
# 会变成 500 小时，训练负荷从 ~50 变成 ~7000。CTL 是 42 天 EMA，一条这样的记录
# 就能把负荷曲线抬高几个月，而且**全程不报错**。宁可拒绝，也不让它进库。
MAX_PLAUSIBLE_DURATION_S = 24 * 3600


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _moving_threshold(sport: str) -> float:
    return 0.8 if sport == "cycling" else 0.5  # m/s


def enrich_samples(samples: list[dict], sport: str = "running"):
    """补齐距离、速度、步幅，返回 (samples, 距离米, 爬升米, 运动秒数)。"""
    pts = sorted([s for s in samples if s.get("t") is not None], key=lambda s: s["t"])
    if len(pts) < 2:
        return pts, 0.0, 0.0, 0.0

    # 同一时间戳只保留最后一个，避免重复导入产生的脏点把 dt 压成 0
    dedup: dict[float, dict] = {}
    for s in pts:
        dedup[s["t"]] = s
    pts = [dedup[t] for t in sorted(dedup)]
    if len(pts) < 2:
        return pts, 0.0, 0.0, 0.0

    distance = 0.0
    elev = 0.0
    moving = 0.0
    prev_alt = None

    for i, s in enumerate(pts):
        if i > 0:
            prev = pts[i - 1]
            dt = s["t"] - prev["t"]
            if dt and dt > 0:
                seg = None
                if s.get("speed") is not None:
                    # 有速度字段时优先采信，GPS 轨迹噪声更大
                    seg = s["speed"] * dt
                elif s.get("lat") is not None and prev.get("lat") is not None:
                    seg = haversine(prev["lat"], prev["lon"], s["lat"], s["lon"])
                if seg and seg > 0:
                    distance += seg
                    if s.get("speed") is None:
                        s["speed"] = seg / dt
                    if seg / dt >= _moving_threshold(sport):
                        moving += dt

        alt = s.get("alt")
        if alt is not None:
            if prev_alt is not None and alt - prev_alt > 0.5:  # 阈值过滤气压噪声
                elev += alt - prev_alt
            prev_alt = alt

        cad, spd = s.get("cad"), s.get("speed")
        if cad and spd and cad > 0:
            s["stride"] = spd * 60.0 / cad

    return pts, distance, elev, moving


def trimp(duration_min: float, avg_hr: float, max_hr: int,
          resting_hr: int, gender: str = "male") -> float:
    """Banister TRIMP 训练负荷。"""
    if not avg_hr or max_hr <= resting_hr or duration_min <= 0:
        return 0.0
    hrr = (avg_hr - resting_hr) / (max_hr - resting_hr)
    hrr = min(max(hrr, 0.0), 1.0)
    if gender == "female":
        return duration_min * hrr * 0.86 * math.exp(1.67 * hrr)
    return duration_min * hrr * 0.64 * math.exp(1.92 * hrr)


def hr_zone_seconds(samples: list[dict], max_hr: int) -> dict[str, float]:
    out = {name: 0.0 for name, _, _ in HR_ZONES}
    for i, s in enumerate(samples):
        hr = s.get("hr")
        if not hr or not max_hr:
            continue
        dt = 0.0
        if i > 0:
            dt = (s["t"] - samples[i - 1]["t"]) or 0.0
            if dt < 0 or dt > 300:
                dt = 1.0
        pct = hr / max_hr
        for name, lo, hi in HR_ZONES:
            if lo <= pct < hi:
                out[name] += dt
                break
    return {k: round(v) for k, v in out.items()}


def hr_drift(samples: list[dict]) -> float | None:
    """后半程相对前半程的心率漂移。正值代表同等强度下心率走高，提示体能衰减或脱水。"""
    if len(samples) < 20:
        return None
    half = len(samples) // 2
    a = [s["hr"] for s in samples[:half] if s.get("hr")]
    b = [s["hr"] for s in samples[half:] if s.get("hr")]
    if not a or not b:
        return None
    return (sum(b) / len(b)) / (sum(a) / len(a)) - 1.0


def met_for(sport: str, avg_speed: float | None) -> float:
    """按运动类型和平均速度估 MET（代谢当量）。

    卡路里和无心率时的负荷都用它，避免两处各写一套导致口径不一致。
    """
    kmh = (avg_speed or 0) * 3.6
    if sport == "cycling":
        return max(4.0, kmh * 0.35)
    if sport == "walking":
        return max(3.0, kmh * 0.5)
    if sport == "swimming":
        return max(6.0, kmh * 2.0)
    return max(6.0, kmh * 0.95)


def estimate_calories(sport: str, avg_speed: float | None,
                      duration_s: float, weight_kg: float | None) -> float | None:
    """基于 MET 的粗估，仅供参考。"""
    if not weight_kg or not duration_s:
        return None
    return round(met_for(sport, avg_speed) * weight_kg * (duration_s / 3600.0), 1)


def hrr_from_met(met: float, max_hr: int, resting_hr: int) -> float | None:
    """由 MET 反推心率储备百分比 %HRR。

    用的是运动生理学里的常用近似 %HRR ≈ (MET - 1) / (MET_max - 1)，
    其中 MET_max 由 VO2max 折算，VO2max 再按经典的最大/静息心率比估。

    这条路只在采样点里完全没有心率时才走，目的是让这类活动也能进
    ATL/CTL 曲线，而不是默默记一个 0 把曲线拉平。精度远不如实测心率，
    所以调用方必须把结果标成 met_estimate，不能和 TRIMP 混为一谈。
    """
    if not met or not max_hr or not resting_hr or max_hr <= resting_hr:
        return None
    vo2max = 15.0 * (max_hr / resting_hr)      # 最大/静息心率比的经典估算
    met_max = vo2max / 3.5
    if met_max <= 1.0:
        return None
    return min(max((met - 1.0) / (met_max - 1.0), 0.0), 1.0)


def summarize_activity(samples: list[dict], sport: str = "running", *,
                       max_hr: int = 190, resting_hr: int = 60,
                       gender: str = "male", weight_kg: float | None = None) -> dict:
    pts, distance, elev, moving = enrich_samples(samples, sport)
    if not pts:
        return {}

    duration = pts[-1]["t"] - pts[0]["t"]
    moving_time = moving or duration

    hrs = [s["hr"] for s in pts if s.get("hr")]
    cads = [s["cad"] for s in pts if s.get("cad")]
    strides = [s.get("stride") for s in pts if s.get("stride")]
    speeds = [s["speed"] for s in pts if s.get("speed")]

    avg_hr = sum(hrs) / len(hrs) if hrs else None
    avg_speed = distance / moving_time if moving_time else None
    pace = (moving_time / (distance / 1000.0)) if distance > 0 and moving_time else None

    # 训练负荷：有心率走 TRIMP，没心率用配速推 %HRR 再走同一套 TRIMP，
    # 这样两种来源量纲一致、可直接混在 ATL/CTL 里。没有来源可靠信息时返回 None。
    if avg_hr:
        load = trimp(moving_time / 60.0, avg_hr, max_hr, resting_hr, gender)
        load_source = "trimp"
    else:
        hrr = hrr_from_met(met_for(sport, avg_speed), max_hr, resting_hr)
        if hrr is not None and moving_time:
            hr_est = resting_hr + hrr * (max_hr - resting_hr)
            load = trimp(moving_time / 60.0, hr_est, max_hr, resting_hr, gender)
            load_source = "met_estimate"
        else:
            load = None
            load_source = "none"

    return {
        "duration_s": round(duration, 1),
        "moving_time_s": round(moving_time, 1),
        "distance_m": round(distance, 1),
        "elev_gain_m": round(elev, 1),
        "avg_hr": round(avg_hr, 1) if avg_hr else None,
        "max_hr": max(hrs) if hrs else None,
        "avg_pace_s_per_km": round(pace, 1) if pace else None,
        "avg_cadence_spm": round(sum(cads) / len(cads), 1) if cads else None,
        "avg_stride_m": round(sum(strides) / len(strides), 3) if strides else None,
        "avg_speed_mps": round(avg_speed, 3) if avg_speed else None,
        "hr_zone_seconds": hr_zone_seconds(pts, max_hr),
        "hr_drift": hr_drift(pts),
        "calories": estimate_calories(sport, avg_speed, duration, weight_kg),
        "load": round(load, 2) if load is not None else None,
        "load_source": load_source,
    }


def _slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else None


def _avg_pace(acts) -> float | None:
    tot_d = sum(a.distance_m or 0 for a in acts)
    tot_t = sum(a.moving_time_s or a.duration_s or 0 for a in acts)
    if tot_d and tot_d > 100:
        return tot_t / (tot_d / 1000.0)
    return None


def weekly_aggregates(acts, as_of: date, weeks: int = 8):
    dist, load, dur = [], [], []
    for w in range(weeks - 1, -1, -1):
        end = as_of - timedelta(days=7 * w)
        start = end - timedelta(days=6)
        sel = [a for a in acts
               if a.start_time and start <= a.start_time.date() <= end]
        dist.append(round(sum(a.distance_m or 0 for a in sel) / 1000.0, 2))
        load.append(round(sum(a.load or 0 for a in sel), 1))
        dur.append(round(sum(a.duration_s or 0 for a in sel) / 3600.0, 2))
    return dist, load, dur


def atl_ctl(daily_load: dict[date, float], as_of: date, history_days: int = 90):
    """指数移动平均：CTL 42 天，ATL 7 天。"""
    ctl = atl = 0.0
    k_ctl = 1 - math.exp(-1 / 42)
    k_atl = 1 - math.exp(-1 / 7)
    cur = as_of - timedelta(days=history_days)
    while cur <= as_of:
        load = daily_load.get(cur, 0.0)
        ctl += (load - ctl) * k_ctl
        atl += (load - atl) * k_atl
        cur += timedelta(days=1)
    return round(ctl, 2), round(atl, 2)


def build_metrics(db, as_of: date | None = None) -> dict:
    """给 Agent 消费的聚合指标。原始采样点绝不出现在这里。"""
    from app.models.fitness import Activity, DailyMetric, Profile

    as_of = as_of or date.today()
    profile = db.query(Profile).first()
    max_hr = (profile.max_hr if profile and profile.max_hr else None) or settings.DEFAULT_MAX_HR
    resting = (profile.resting_hr if profile and profile.resting_hr else None) or settings.DEFAULT_RESTING_HR
    gender = (profile.gender if profile else "male") or "male"

    since = datetime.combine(as_of - timedelta(days=90), datetime.min.time())
    acts = db.query(Activity).filter(Activity.start_time >= since).all()

    daily: dict[date, float] = {}
    for a in acts:
        if a.start_time:
            daily[a.start_time.date()] = daily.get(a.start_time.date(), 0.0) + (a.load or 0.0)
    ctl, atl = atl_ctl(daily, as_of)

    w_dist, w_load, w_dur = weekly_aggregates(acts, as_of, 8)

    def between(days_from: int, days_to: int):
        lo = datetime.combine(as_of - timedelta(days=days_from), datetime.min.time())
        hi = datetime.combine(as_of - timedelta(days=days_to), datetime.min.time())
        return [a for a in acts if a.start_time and lo <= a.start_time < hi]

    recent14, prior14 = between(14, 0), between(28, 14)
    p_now, p_prev = _avg_pace(recent14), _avg_pace(prior14)
    pace_trend = ((p_now / p_prev - 1) * 100) if (p_now and p_prev) else 0.0

    last28 = between(28, 0)
    hrs = [a.avg_hr for a in last28 if a.avg_hr]
    cads = [a.avg_cadence_spm for a in last28 if a.avg_cadence_spm]
    strides = [a.avg_stride_m for a in last28 if a.avg_stride_m]
    zone_totals: dict[str, float] = {z: 0.0 for z, _, _ in HR_ZONES}
    for a in last28:
        for k, v in (a.hr_zone_json or {}).items():
            zone_totals[k] = zone_totals.get(k, 0.0) + v

    days = db.query(DailyMetric).filter(
        DailyMetric.date >= as_of - timedelta(days=30)
    ).order_by(DailyMetric.date).all()

    xs = [(d.date - as_of).days for d in days if d.weight_kg]
    ys = [d.weight_kg for d in days if d.weight_kg]
    weight_slope = _slope(xs, ys)
    weight_trend = (weight_slope * 7) if weight_slope is not None else 0.0

    rhr_x = [(d.date - as_of).days for d in days if d.resting_hr]
    rhr_y = [d.resting_hr for d in days if d.resting_hr]
    rhr_slope = _slope(rhr_x, rhr_y)

    sleeps = [d.sleep_h for d in days if d.sleep_h]
    rpes, pains = [], []
    for a in last28:
        fb = a.feedback
        if fb:
            if fb.rpe:
                rpes.append(fb.rpe)
            if fb.pain and fb.pain.strip():
                pains.append(fb.pain.strip())

    recent = sorted(acts, key=lambda a: a.start_time or datetime.min, reverse=True)[:10]

    # load_source 没有落库，靠「有负荷但没平均心率」反推是不是配速估算的，
    # 和 routers/activities.py::load_source_of 用同一套判据。
    n28 = len(last28)
    est_load = sum((a.load or 0.0) for a in last28 if a.load and not a.avg_hr)
    load_28 = sum((a.load or 0.0) for a in last28)
    hr_missing_ratio = round(1 - len(hrs) / n28, 2) if n28 else None
    estimated_load_ratio = round(est_load / load_28, 2) if load_28 else None

    return {
        "as_of": as_of.isoformat(),
        "ctl": ctl,
        "atl": atl,
        "tsb": round(ctl - atl, 2),
        "acwr": round(atl / ctl, 2) if ctl > 0 else 0.0,
        "weekly_distance_km": w_dist,
        "weekly_load": w_load,
        "weekly_duration_h": w_dur,
        "avg_pace_s_per_km": round(p_now, 1) if p_now else None,
        "pace_trend_pct": round(pace_trend, 2),
        "avg_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
        "hr_zone_seconds": zone_totals,
        "avg_cadence_spm": round(sum(cads) / len(cads), 1) if cads else None,
        "avg_stride_m": round(sum(strides) / len(strides), 3) if strides else None,
        "weight_trend_kg_per_week": round(weight_trend, 3),
        "sleep_avg_h": round(sum(sleeps) / len(sleeps), 2) if sleeps else None,
        "resting_hr_trend": round(rhr_slope * 7, 2) if rhr_slope is not None else 0.0,
        "avg_rpe": round(sum(rpes) / len(rpes), 1) if rpes else None,
        "pain_flags": sorted(set(pains))[:5],
        "activity_count_28d": n28,
        # 数据质量。华为 GPX/TCX 导出的活动没有心率，负荷是配速估算的；
        # 下游（教练评估 + 计划硬校验）会拿 acwr / TSB 做判断，
        # 必须让它知道这些数字里有多少是估的，不能当成实测值用。
        "hr_missing_ratio_28d": hr_missing_ratio,
        "estimated_load_ratio_28d": estimated_load_ratio,
        "longest_recent_min": round(max((a.duration_s or 0) for a in last28) / 60.0, 1) if last28 else 0.0,
    }


def activity_cards(acts) -> list[dict]:
    """给 Agent 的精简卡片，不含采样点。"""
    out = []
    for a in acts:
        fb = a.feedback
        out.append({
            "date": a.start_time.strftime("%Y-%m-%d") if a.start_time else None,
            "type": a.sport_type,
            "distance_km": round((a.distance_m or 0) / 1000.0, 2),
            "duration_min": round((a.duration_s or 0) / 60.0, 1),
            "avg_hr": a.avg_hr,
            "pace_min_per_km": round(a.avg_pace_s_per_km / 60.0, 2) if a.avg_pace_s_per_km else None,
            "rpe": fb.rpe if fb else None,
        })
    return out
