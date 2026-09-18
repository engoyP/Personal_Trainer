from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models.fitness import Activity, ActivitySample, Profile
from app.services import analytics
from app.services.parsers import parse_file
from app.services.parsers.sport import guess_sport


def profile_params(db: Session):
    from app.models.fitness import DailyMetric

    p = db.query(Profile).first()
    # 当前体重取最近一次记录的每日身体数据，而不是目标体重
    latest = db.query(DailyMetric).order_by(DailyMetric.date.desc()).first()
    return {
        "max_hr": (p.max_hr if p and p.max_hr else None) or settings.DEFAULT_MAX_HR,
        "resting_hr": (p.resting_hr if p and p.resting_hr else None) or settings.DEFAULT_RESTING_HR,
        "gender": (p.gender if p and p.gender else "male") or "male",
        "weight_kg": latest.weight_kg if latest else None,
    }


def ingest_bytes(db: Session, filename: str, content: bytes) -> dict:
    """解析文件 -> 计算指标 -> 落库。返回导入结果。"""
    parsed = parse_file(filename, content)
    samples = parsed.get("samples") or []
    sport = parsed.get("sport_type") or "running"
    start = parsed.get("start_time")

    if not samples:
        # 解析器知道得更清楚（比如「这是每天一行的汇总表」），优先用它的说法，
        # 不然用户只会看到一句没头没尾的「没有采样点」。
        return {"ok": False,
                "reason": parsed.get("error") or "文件里没有解析到采样点",
                "activity_id": None}

    # 去重：同一来源同一开始时间视为重复
    if start:
        exists = db.query(Activity).filter(
            Activity.start_time == start,
            Activity.sport_type == sport,
        ).first()
        if exists:
            return {"ok": False, "reason": "这条运动已存在", "activity_id": exists.id, "duplicate": True}

    summary = analytics.summarize_activity(samples, sport, **profile_params(db))
    if not summary:
        return {"ok": False, "reason": "采样点不足，无法计算指标", "activity_id": None}

    # 时长明显不合常理 → 拒绝，别让它进库。
    # 最典型的成因是文件里的时间字段不是秒（毫秒会被放大 1000 倍）：一次 30 分钟
    # 的运动变成 500 小时，负荷从 ~50 变成 ~7000，而 CTL 是 42 天 EMA ——
    # 一条这样的记录能把负荷曲线抬高几个月，曲线自此失去意义。
    if summary["duration_s"] > analytics.MAX_PLAUSIBLE_DURATION_S:
        hours = summary["duration_s"] / 3600.0
        return {
            "ok": False,
            "activity_id": None,
            "reason": (
                f"算出的时长是 {hours:.0f} 小时，不合常理，已拒绝导入。"
                "最常见的原因是文件里的时间字段单位不是秒（例如是毫秒），"
                "请核对导出文件的字段含义后重新导入。"
            ),
        }

    act = Activity(
        source=filename.rsplit(".", 1)[-1].lower() if "." in filename else "manual",
        external_id=parsed.get("external_id"),
        sport_type=sport,
        start_time=start,
        duration_s=summary["duration_s"],
        moving_time_s=summary["moving_time_s"],
        distance_m=summary["distance_m"],
        elev_gain_m=summary["elev_gain_m"],
        avg_hr=summary["avg_hr"],
        max_hr=summary["max_hr"],
        avg_pace_s_per_km=summary["avg_pace_s_per_km"],
        avg_cadence_spm=summary["avg_cadence_spm"],
        avg_stride_m=summary["avg_stride_m"],
        avg_speed_mps=summary["avg_speed_mps"],
        calories=summary["calories"],
        load=summary["load"],
        hr_zone_json=summary.get("hr_zone_seconds"),
        file_name=filename,
    )
    db.add(act)
    db.flush()

    for s in samples:
        db.add(ActivitySample(
            activity_id=act.id,
            t_s=s.get("t"),
            lat=s.get("lat"),
            lon=s.get("lon"),
            altitude=s.get("alt"),
            heart_rate=s.get("hr"),
            cadence=s.get("cad"),
            speed=s.get("speed"),
            stride_m=s.get("stride"),
        ))

    db.commit()
    out = {
        "ok": True,
        "activity_id": act.id,
        "duplicate": False,
        "sample_count": len(samples),
        "summary": summary,
    }
    # 华为全量导出的 JSON 里往往有几十上百条运动，而这里一次只写一条。
    # 不吭声的话用户会以为全导进去了 —— 必须把丢掉的部分说出来。
    notes = list(parsed.get("warnings") or [])
    importable = int(parsed.get("records_importable") or 0)
    if importable > 1:
        note = (
            f"文件里识别出 {importable} 条可用运动（共 {parsed.get('records_found')} 条记录），"
            "本次只导入了采样点最多的 1 条。"
        )
        if parsed.get("records_truncated"):
            note += "记录数超过 200 条，只检查了前 200 条。"
        notes.append(note)
    if notes:
        out["warning"] = " ".join(notes)
    return out


def create_manual_activity(
    db: Session,
    *,
    sport_type: str,
    start_time: datetime,
    duration_min: float,
    distance_km: float,
    avg_hr: float | None = None,
    max_hr: float | None = None,
    avg_cadence_spm: float | None = None,
    avg_stride_m: float | None = None,
    elev_gain_m: float | None = None,
    calories: float | None = None,
) -> dict:
    """从 summary 字段手工创建一条运动记录。

    没有逐点采样，所以：
      - 心率区间 hr_zone_json 留空；
      - 有心率时负荷走 TRIMP，没心率时按运动类型 + 平均速度估算；
      - 移动时间默认等于总时长（用户没提供暂停信息）。
    """
    sport = (guess_sport(sport_type) or "running").lower()
    duration_s = duration_min * 60.0
    distance_m = distance_km * 1000.0

    if duration_s > analytics.MAX_PLAUSIBLE_DURATION_S:
        return {
            "ok": False,
            "activity_id": None,
            "reason": (
                f"时长为 {duration_s / 3600.0:.0f} 小时，超过单日上限，请检查输入。"
            ),
        }

    if distance_m > 500_000:
        return {
            "ok": False,
            "activity_id": None,
            "reason": "单次距离超过 500 km，请检查单位是否为公里。",
        }

    exists = db.query(Activity).filter(
        Activity.start_time == start_time,
        Activity.sport_type == sport,
    ).first()
    if exists:
        return {"ok": False, "reason": "这条运动已存在", "activity_id": exists.id,
                "duplicate": True}

    moving_time_s = duration_s
    avg_speed = distance_m / moving_time_s if moving_time_s and distance_m > 0 else None
    pace = (
        moving_time_s / (distance_m / 1000.0)
        if distance_m > 0 and moving_time_s else None
    )

    p = profile_params(db)
    if avg_hr:
        load = analytics.trimp(
            moving_time_s / 60.0, avg_hr, p["max_hr"], p["resting_hr"], p["gender"]
        )
        load_source = "trimp"
    else:
        met = analytics.met_for(sport, avg_speed)
        hrr = analytics.hrr_from_met(met, p["max_hr"], p["resting_hr"])
        if hrr is not None and moving_time_s:
            hr_est = p["resting_hr"] + hrr * (p["max_hr"] - p["resting_hr"])
            load = analytics.trimp(
                moving_time_s / 60.0, hr_est, p["max_hr"], p["resting_hr"], p["gender"]
            )
            load_source = "met_estimate"
        else:
            load = None
            load_source = "none"

    if calories is None and avg_speed and duration_s and p["weight_kg"]:
        calories = analytics.estimate_calories(sport, avg_speed, duration_s, p["weight_kg"])

    act = Activity(
        source="manual",
        sport_type=sport,
        start_time=start_time,
        duration_s=round(duration_s, 1),
        moving_time_s=round(moving_time_s, 1),
        distance_m=round(distance_m, 1),
        elev_gain_m=elev_gain_m,
        avg_hr=round(avg_hr, 1) if avg_hr else None,
        max_hr=max_hr,
        avg_pace_s_per_km=round(pace, 1) if pace else None,
        avg_cadence_spm=avg_cadence_spm,
        avg_stride_m=avg_stride_m,
        avg_speed_mps=round(avg_speed, 3) if avg_speed else None,
        calories=calories,
        load=round(load, 2) if load is not None else None,
        hr_zone_json=None,
        file_name=None,
    )
    db.add(act)
    db.flush()
    db.commit()

    summary = {
        "distance_m": act.distance_m,
        "duration_s": act.duration_s,
        "moving_time_s": act.moving_time_s,
        "avg_hr": act.avg_hr,
        "max_hr": act.max_hr,
        "avg_pace_s_per_km": act.avg_pace_s_per_km,
        "avg_speed_mps": act.avg_speed_mps,
        "avg_cadence_spm": act.avg_cadence_spm,
        "avg_stride_m": act.avg_stride_m,
        "calories": act.calories,
        "load": act.load,
        "load_source": load_source,
    }
    out = {
        "ok": True,
        "activity_id": act.id,
        "duplicate": False,
        "summary": summary,
    }
    if not avg_hr:
        out["warning"] = (
            "手动录入无逐点心率，训练负荷由配速估算，心率区间留空。"
        )
    return out
