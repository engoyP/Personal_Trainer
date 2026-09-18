from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.fitness import Activity
from app.services import analytics

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary")
def summary(days: int = 30, sport: str | None = None, db: Session = Depends(get_db)):
    since = datetime.now() - timedelta(days=days)
    q = db.query(Activity).filter(Activity.start_time >= since)
    if sport:
        q = q.filter(Activity.sport_type == sport)
    acts = q.all()

    if not acts:
        return {
            "days": days, "count": 0, "total_distance_km": 0,
            "total_duration_h": 0, "avg_pace_s_per_km": None,
            "avg_hr": None, "avg_cadence_spm": None, "avg_stride_m": None,
            "total_load": 0, "total_calories": 0,
        }

    dist = sum(a.distance_m or 0 for a in acts)
    moving = sum(a.moving_time_s or a.duration_s or 0 for a in acts)
    dur = sum(a.duration_s or 0 for a in acts)

    hrs = [(a.avg_hr, a.duration_s or 0) for a in acts if a.avg_hr]
    cads = [a.avg_cadence_spm for a in acts if a.avg_cadence_spm]
    strides = [a.avg_stride_m for a in acts if a.avg_stride_m]

    return {
        "days": days,
        "count": len(acts),
        "total_distance_km": round(dist / 1000.0, 2),
        "total_duration_h": round(dur / 3600.0, 2),
        "avg_pace_s_per_km": round(moving / (dist / 1000.0), 1) if dist > 100 else None,
        "avg_hr": round(sum(h * w for h, w in hrs) / sum(w for _, w in hrs), 1) if hrs else None,
        "avg_cadence_spm": round(sum(cads) / len(cads), 1) if cads else None,
        "avg_stride_m": round(sum(strides) / len(strides), 3) if strides else None,
        "total_load": round(sum(a.load or 0 for a in acts), 1),
        "total_calories": round(sum(a.calories or 0 for a in acts), 1),
    }


@router.get("/trend")
def trend(days: int = 90, db: Session = Depends(get_db)):
    since = datetime.now() - timedelta(days=days)
    acts = db.query(Activity).filter(Activity.start_time >= since).order_by(Activity.start_time).all()

    by_day: dict[str, dict] = defaultdict(lambda: {"distance_m": 0.0, "duration_s": 0.0,
                                                   "load": 0.0, "hrs": [], "paces": []})
    for a in acts:
        if not a.start_time:
            continue
        key = a.start_time.strftime("%Y-%m-%d")
        d = by_day[key]
        d["distance_m"] += a.distance_m or 0
        d["duration_s"] += a.duration_s or 0
        d["load"] += a.load or 0
        if a.avg_hr:
            d["hrs"].append(a.avg_hr)
        if a.avg_pace_s_per_km:
            d["paces"].append(a.avg_pace_s_per_km)

    items = []
    for k in sorted(by_day):
        d = by_day[k]
        items.append({
            "date": k,
            "distance_km": round(d["distance_m"] / 1000.0, 2),
            "duration_min": round(d["duration_s"] / 60.0, 1),
            "load": round(d["load"], 1),
            "avg_hr": round(sum(d["hrs"]) / len(d["hrs"]), 1) if d["hrs"] else None,
            "avg_pace_s_per_km": round(sum(d["paces"]) / len(d["paces"]), 1) if d["paces"] else None,
        })
    return {"items": items}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    """给 Agent 消费的聚合指标，也是前端诊断面板的数据源。"""
    return analytics.build_metrics(db)


@router.get("/load")
def load_curve(days: int = 90, db: Session = Depends(get_db)):
    """ATL / CTL / TSB 曲线。"""
    from datetime import date, timedelta as td  # noqa: F401

    from app.models.fitness import Activity as A

    as_of = datetime.now().date()
    since = datetime.combine(as_of - td(days=days), datetime.min.time())
    acts = db.query(A).filter(A.start_time >= since).all()

    daily: dict = defaultdict(float)
    for a in acts:
        if a.start_time:
            daily[a.start_time.date()] += a.load or 0

    items, ctl, atl = [], 0.0, 0.0
    import math

    k_ctl = 1 - math.exp(-1 / 42)
    k_atl = 1 - math.exp(-1 / 7)
    cur = as_of - td(days=days)
    while cur <= as_of:
        v = daily.get(cur, 0.0)
        ctl += (v - ctl) * k_ctl
        atl += (v - atl) * k_atl
        items.append({
            "date": cur.isoformat(),
            "load": round(v, 1),
            "ctl": round(ctl, 2),
            "atl": round(atl, 2),
            "tsb": round(ctl - atl, 2),
        })
        cur += td(days=1)

    return {"items": items}


@router.post("/recalc")
def recalc(db: Session = Depends(get_db)):
    """按当前档案重算全部活动的负荷、卡路里与心率区间。

    改了体重或最大心率后需要调用，历史数据才会跟着更新。
    """
    from app.models.fitness import ActivitySample
    from app.services.ingest import profile_params

    params = profile_params(db)
    acts = db.query(Activity).all()

    for a in acts:
        rows = (
            db.query(ActivitySample)
            .filter(ActivitySample.activity_id == a.id)
            .order_by(ActivitySample.t_s)
            .all()
        )
        if not rows:
            continue

        samples = [
            {"t": r.t_s, "lat": r.lat, "lon": r.lon, "alt": r.altitude,
             "hr": r.heart_rate, "cad": r.cadence, "speed": r.speed}
            for r in rows
        ]
        s = analytics.summarize_activity(samples, a.sport_type, **params)
        if not s:
            continue

        # 采样点里没有心率、但汇总上有平均心率 —— 这个心率只能是用户手工补填的
        # （华为 GPX/TCX 导出的文件里根本没有心率字段）。重算必须保留它，
        # 否则用户辛苦填的数字一次 recalc 就没了，还会静默退回配速估算。
        if not any(r.heart_rate for r in rows) and a.avg_hr:
            s["avg_hr"] = a.avg_hr
            s["max_hr"] = a.max_hr or s["max_hr"]
            moving_min = (a.moving_time_s or a.duration_s or 0) / 60.0
            load = analytics.trimp(moving_min, a.avg_hr, params["max_hr"],
                                   params["resting_hr"], params["gender"])
            s["load"] = round(load, 2) if load else None

        a.duration_s = s["duration_s"]
        a.moving_time_s = s["moving_time_s"]
        a.distance_m = s["distance_m"]
        a.elev_gain_m = s["elev_gain_m"]
        a.avg_hr = s["avg_hr"]
        a.max_hr = s["max_hr"]
        a.avg_pace_s_per_km = s["avg_pace_s_per_km"]
        a.avg_cadence_spm = s["avg_cadence_spm"]
        a.avg_stride_m = s["avg_stride_m"]
        a.avg_speed_mps = s["avg_speed_mps"]
        a.calories = s["calories"]
        a.load = s["load"]
        a.hr_zone_json = s["hr_zone_seconds"]

    db.commit()
    return {"ok": True, "recalculated": len(acts)}
