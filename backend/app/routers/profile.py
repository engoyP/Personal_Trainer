from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.fitness import Activity, ActivityFeedback, DailyMetric, Profile

router = APIRouter(prefix="/api/profile", tags=["profile"])
feedback_router = APIRouter(prefix="/api/activities", tags=["feedback"])


class ProfileIn(BaseModel):
    name: str | None = None
    gender: str | None = None
    birth_year: int | None = None
    height_cm: float | None = None
    max_hr: int | None = None
    resting_hr: int | None = None
    goal: str | None = None
    target_weight_kg: float | None = None
    weekly_available_days: int | None = None
    experience: str | None = None
    injury_notes: str | None = None


class DailyIn(BaseModel):
    date: str
    weight_kg: float | None = None
    body_fat_pct: float | None = None
    sleep_h: float | None = None
    sleep_quality: int | None = None
    resting_hr: int | None = None
    note: str | None = None


class FeedbackIn(BaseModel):
    rpe: int | None = None
    fatigue: int | None = None
    soreness: int | None = None
    mood: str | None = None
    pain: str | None = None
    note: str | None = None


def _dump(p: Profile) -> dict:
    return {
        "id": p.id, "name": p.name, "gender": p.gender, "birth_year": p.birth_year,
        "height_cm": p.height_cm, "max_hr": p.max_hr, "resting_hr": p.resting_hr,
        "goal": p.goal, "target_weight_kg": p.target_weight_kg,
        "weekly_available_days": p.weekly_available_days, "experience": p.experience,
        "injury_notes": p.injury_notes,
    }


@router.get("")
def get_profile(db: Session = Depends(get_db)):
    p = db.query(Profile).first()
    if not p:
        p = Profile()
        db.add(p)
        db.commit()
        db.refresh(p)
    return _dump(p)


@router.put("")
def update_profile(data: ProfileIn, db: Session = Depends(get_db)):
    p = db.query(Profile).first()
    if not p:
        p = Profile()
        db.add(p)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _dump(p)


@router.get("/daily")
def list_daily(days: int = 30, db: Session = Depends(get_db)):
    since = date.today() - __import__("datetime").timedelta(days=days)
    rows = db.query(DailyMetric).filter(DailyMetric.date >= since).order_by(DailyMetric.date).all()
    return {
        "items": [
            {
                "date": r.date.isoformat(), "weight_kg": r.weight_kg,
                "body_fat_pct": r.body_fat_pct, "sleep_h": r.sleep_h,
                "sleep_quality": r.sleep_quality, "resting_hr": r.resting_hr,
                "note": r.note,
            }
            for r in rows
        ]
    }


@router.post("/daily")
def upsert_daily(data: DailyIn, db: Session = Depends(get_db)):
    d = date.fromisoformat(data.date)
    r = db.get(DailyMetric, d)
    if not r:
        r = DailyMetric(date=d)
        db.add(r)
    for k, v in data.model_dump(exclude_none=True).items():
        if k != "date":
            setattr(r, k, v)
    db.commit()
    return {"ok": True, "date": data.date}


@feedback_router.post("/{activity_id}/feedback")
def upsert_feedback(activity_id: int, data: FeedbackIn, db: Session = Depends(get_db)):
    a = db.get(Activity, activity_id)
    if not a:
        raise HTTPException(404, "运动记录不存在")

    fb = a.feedback
    if not fb:
        fb = ActivityFeedback(activity_id=activity_id)
        db.add(fb)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(fb, k, v)
    db.commit()
    return {"ok": True, "activity_id": activity_id}
