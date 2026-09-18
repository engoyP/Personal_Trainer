from __future__ import annotations

import base64
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.fitness import Activity, ActivitySample
from app.services import analytics
from app.services.ingest import create_manual_activity, ingest_bytes, profile_params
from app.services.parsers import probe_file
from app.services.ocr_client import ocr_image

router = APIRouter(prefix="/api/activities", tags=["activities"])

MAX_POINTS = 600


def _downsample(rows: list, max_points: int = MAX_POINTS):
    n = len(rows)
    if n <= max_points:
        return rows
    step = n / max_points
    out, i = [], 0.0
    while int(i) < n:
        out.append(rows[int(i)])
        i += step
    return out


def load_source_of(a: Activity) -> str | None:
    """负荷从哪来。不额外存字段，直接由是否有心率反推——
    有心率算出来的是 TRIMP，没心率的是配速估算，两者绝不能混淆。
    """
    if not a.load:
        return None
    return "trimp" if a.avg_hr else "met_estimate"


def _brief(a: Activity) -> dict:
    fb = a.feedback
    return {
        "id": a.id,
        "sport_type": a.sport_type,
        "start_time": a.start_time.isoformat() if a.start_time else None,
        "distance_km": round((a.distance_m or 0) / 1000.0, 2),
        "duration_min": round((a.duration_s or 0) / 60.0, 1),
        "moving_min": round((a.moving_time_s or 0) / 60.0, 1),
        "avg_pace_s_per_km": a.avg_pace_s_per_km,
        "avg_hr": a.avg_hr,
        "max_hr": a.max_hr,
        "avg_cadence_spm": a.avg_cadence_spm,
        "avg_stride_m": a.avg_stride_m,
        "elev_gain_m": a.elev_gain_m,
        "load": a.load,
        "load_source": load_source_of(a),
        "calories": a.calories,
        "source": a.source,
        "file_name": a.file_name,
        "rpe": fb.rpe if fb else None,
    }


@router.get("")
def list_activities(limit: int = 50, offset: int = 0, sport: str | None = None,
                    year: int | None = None, month: int | None = None,
                    db: Session = Depends(get_db)):
    q = db.query(Activity).order_by(desc(Activity.start_time))
    if sport:
        q = q.filter(Activity.sport_type == sport)
    if year and month:
        start = datetime(year, month, 1)
        end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        q = q.filter(Activity.start_time >= start, Activity.start_time < end)
    total = q.count()
    return {"total": total, "items": [_brief(a) for a in q.offset(offset).limit(limit).all()]}


@router.get("/calendar")
def activity_calendar(year: int | None = None, month: int | None = None,
                      db: Session = Depends(get_db)):
    """当月（缺省）或指定年月每天的运动汇总，供首页日历标签用。

    返回 { "days": {"2026-09-05": [{"sport_type": "running", "distance_km": 5.68}] } }
    """
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    rows = (
        db.query(Activity)
        .filter(Activity.start_time >= start, Activity.start_time < end)
        .order_by(Activity.start_time)
        .all()
    )
    days: dict[str, list[dict]] = {}
    for a in rows:
        d = a.start_time.strftime("%Y-%m-%d")
        days.setdefault(d, []).append({
            "id": a.id,
            "sport_type": a.sport_type or "running",
            "distance_km": round((a.distance_m or 0) / 1000.0, 2),
        })
    return {"year": year, "month": month, "days": days}


@router.get("/{activity_id}/track")
def get_track(activity_id: int, max_points: int = 800, db: Session = Depends(get_db)):
    a = db.get(Activity, activity_id)
    if not a:
        raise HTTPException(404, "运动记录不存在")

    rows = (
        db.query(ActivitySample)
        .filter(ActivitySample.activity_id == activity_id, ActivitySample.lat.isnot(None))
        .order_by(ActivitySample.t_s)
        .all()
    )
    picked = _downsample(rows, max_points)
    return {
        "id": activity_id,
        "count": len(picked),
        "has_track": len(rows) >= 2,
        "points": [
            {"lat": s.lat, "lon": s.lon, "hr": s.heart_rate, "t": s.t_s}
            for s in picked
        ],
    }


@router.get("/{activity_id}")
def get_activity(activity_id: int, db: Session = Depends(get_db)):
    a = db.get(Activity, activity_id)
    if not a:
        raise HTTPException(404, "运动记录不存在")

    samples = (
        db.query(ActivitySample)
        .filter(ActivitySample.activity_id == activity_id)
        .order_by(ActivitySample.t_s)
        .all()
    )
    picked = _downsample(samples)

    series = []
    for s in picked:
        pace = round(1000.0 / s.speed, 1) if (s.speed and s.speed > 0.3) else None
        series.append({
            "t": s.t_s,
            "hr": s.heart_rate,
            "pace": pace,
            "cad": s.cadence,
            "stride": s.stride_m,
            "alt": s.altitude,
        })

    fb = a.feedback
    return {
        **_brief(a),
        "sample_count": len(samples),
        # 文件本身有没有逐点心率。前端靠它决定要不要显示补填卡片 ——
        # 不能只用 load_source 判断：用户填完心率后 load_source 变 trimp，
        # 卡片会消失，填错了就改不了也清不掉。
        "has_sample_hr": any(s.heart_rate for s in samples),
        "series": series,
        "feedback": (
            {
                "rpe": fb.rpe, "fatigue": fb.fatigue, "soreness": fb.soreness,
                "mood": fb.mood, "pain": fb.pain, "note": fb.note,
            }
            if fb else None
        ),
    }


class HeartRateIn(BaseModel):
    avg_hr: float | None = None
    max_hr: float | None = None


class ManualActivityIn(BaseModel):
    sport_type: str
    start_time: datetime
    duration_min: float
    distance_km: float
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cadence_spm: float | None = None
    avg_stride_m: float | None = None
    elev_gain_m: float | None = None
    calories: float | None = None


class RecognizeIn(BaseModel):
    image_base64: str


@router.post("/{activity_id}/heart-rate")
def set_heart_rate(activity_id: int, payload: HeartRateIn,
                   db: Session = Depends(get_db)):
    """补填平均/最大心率，专给华为 GPX/TCX 这类导不出心率的文件用。

    填了之后训练负荷从配速估算（met_estimate）切成真正的 TRIMP。
    但只改汇总值，**不伪造逐点心率** —— 心率区间仍按采样点算，
    文件本身没心率，区间就依然是空的，不会拿一个平均值编出分布来。

    传 null 表示清空（用 model_fields_set 区分"没传"和"传了 null"）。
    """
    a = db.get(Activity, activity_id)
    if not a:
        raise HTTPException(404, "运动记录不存在")

    def _check(v, name):
        if v is not None and not (30 <= v <= 250):
            raise HTTPException(400, f"{name} 应在 30~250 之间，收到 {v}")

    _check(payload.avg_hr, "平均心率")
    _check(payload.max_hr, "最大心率")

    if "avg_hr" in payload.model_fields_set:
        a.avg_hr = payload.avg_hr
    if "max_hr" in payload.model_fields_set:
        a.max_hr = payload.max_hr
    if a.avg_hr and a.max_hr and a.max_hr < a.avg_hr:
        raise HTTPException(400, "最大心率不能小于平均心率")

    p = profile_params(db)

    if a.avg_hr:
        # 有心率：走真正的 TRIMP
        moving_min = (a.moving_time_s or a.duration_s or 0) / 60.0
        load = analytics.trimp(moving_min, a.avg_hr, p["max_hr"],
                               p["resting_hr"], p["gender"])
        a.load = round(load, 2) if load else None
    else:
        # 清空后要落回配速估算，不能留 None —— 否则这条在负荷曲线上直接消失，
        # 和"导入时算出 44.5"的表现不一致，用户会以为数据丢了。
        rows = (
            db.query(ActivitySample)
            .filter(ActivitySample.activity_id == a.id)
            .order_by(ActivitySample.t_s)
            .all()
        )
        if rows:
            samples = [
                {"t": r.t_s, "lat": r.lat, "lon": r.lon, "alt": r.altitude,
                 "hr": r.heart_rate, "cad": r.cadence, "speed": r.speed}
                for r in rows
            ]
            s = analytics.summarize_activity(samples, a.sport_type, **p)
            a.load = s.get("load") if s else None
        else:
            a.load = None

    db.commit()
    return _brief(a)


@router.delete("/{activity_id}")
def delete_activity(activity_id: int, db: Session = Depends(get_db)):
    a = db.get(Activity, activity_id)
    if not a:
        raise HTTPException(404, "运动记录不存在")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/manual")
def create_manual(payload: ManualActivityIn, db: Session = Depends(get_db)):
    """没有 GPS/心率文件时，从 summary 图手工录入一条运动。"""
    if payload.avg_hr is not None and payload.max_hr is not None:
        if payload.max_hr < payload.avg_hr:
            raise HTTPException(400, "最大心率不能小于平均心率")
    for name, v in (("平均心率", payload.avg_hr), ("最大心率", payload.max_hr)):
        if v is not None and not (30 <= v <= 250):
            raise HTTPException(400, f"{name} 应在 30~250 之间，收到 {v}")

    try:
        return create_manual_activity(
            db,
            sport_type=payload.sport_type,
            start_time=payload.start_time,
            duration_min=payload.duration_min,
            distance_km=payload.distance_km,
            avg_hr=payload.avg_hr,
            max_hr=payload.max_hr,
            avg_cadence_spm=payload.avg_cadence_spm,
            avg_stride_m=payload.avg_stride_m,
            elev_gain_m=payload.elev_gain_m,
            calories=payload.calories,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/recognize")
def recognize(payload: RecognizeIn):
    """识图录入第一步：把运动海报图片识别成 summary 字段（不直接落库）。

    识别结果由前端展示给用户逐字段核对，确认后再调 /manual 落库。
    OCR 服务不可用则降级：返回 ok=False + error，前端提示改用手动录入。
    """
    b64 = payload.image_base64
    if "," in b64:
        b64 = b64.split(",", 1)[1]          # 去掉 data:image/...;base64, 前缀
    try:
        data = base64.b64decode(b64)
    except Exception:                       # noqa: BLE001
        raise HTTPException(400, "图片数据无法解码")
    ocr = ocr_image(data)
    if not ocr.get("ok"):
        return {"ok": False, "error": ocr.get("error", "OCR 失败"),
                "block_count": len(ocr.get("blocks", [])), "fields": None}
    from app.services.recognition import recognize_activity

    result = recognize_activity(ocr["blocks"])
    result["ok"] = True
    result["block_count"] = len(ocr["blocks"])
    return result


@router.post("/upload")
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "文件为空")
    try:
        return ingest_bytes(db, file.filename or "upload", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"解析失败：{e}")


@router.post("/probe")
async def probe(file: UploadFile = File(...)):
    """导入前先看结构，确认识别是否正确。"""
    content = await file.read()
    try:
        return probe_file(file.filename or "upload", content)
    except Exception as e:
        raise HTTPException(400, f"无法识别文件：{e}")
