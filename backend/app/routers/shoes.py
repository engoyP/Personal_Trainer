"""跑鞋管理：录入跑鞋、上传照片、记每日跑量、累计陪跑里程。

图片存本地 data/shoe_images/，通过 GET /{id}/image 返回，不依赖外部存储。
"""
from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import DATA_DIR, settings
from app.database import get_db
from app.models.fitness import Shoe, ShoeMileage

router = APIRouter(prefix="/api/shoes", tags=["shoes"])

_IMG_DIR = Path(DATA_DIR) / "shoe_images"
_IMG_DIR.mkdir(parents=True, exist_ok=True)


class ShoeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    brand: str = ""
    purchased_date: str | None = None      # YYYY-MM-DD
    initial_km: float = 0.0
    note: str = ""


class MileageIn(BaseModel):
    run_date: str = Field(..., description="YYYY-MM-DD")
    km: float = Field(..., gt=0, le=200)
    note: str = ""


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{s}")


def _shoe_out(s: Shoe) -> dict:
    total = sum((m.km or 0) for m in s.mileages) + (s.initial_km or 0)
    return {
        "id": s.id,
        "name": s.name,
        "brand": s.brand,
        "has_image": bool(s.image_path),
        "purchased_date": s.purchased_date.isoformat() if s.purchased_date else None,
        "initial_km": s.initial_km,
        "retired": s.retired,
        "note": s.note,
        "total_km": round(total, 2),
        "mileage_count": len(s.mileages),
    }


@router.get("")
def list_shoes(db: Session = Depends(get_db)):
    rows = db.query(Shoe).order_by(Shoe.retired.asc(), Shoe.created_at.desc()).all()
    return {"items": [_shoe_out(s) for s in rows]}


@router.post("")
def create_shoe(payload: ShoeIn, db: Session = Depends(get_db)):
    s = Shoe(
        name=payload.name.strip(),
        brand=payload.brand.strip(),
        purchased_date=_parse_date(payload.purchased_date),
        initial_km=payload.initial_km or 0,
        note=payload.note,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _shoe_out(s)


class ShoePatch(BaseModel):
    name: str | None = Field(None, max_length=128)
    brand: str | None = None
    purchased_date: str | None = None
    initial_km: float | None = None
    retired: int | None = None
    note: str | None = None


@router.patch("/{shoe_id}")
def update_shoe(shoe_id: int, payload: ShoePatch, db: Session = Depends(get_db)):
    s = db.get(Shoe, shoe_id)
    if not s:
        raise HTTPException(404, "跑鞋不存在")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "鞋名不能为空")
        s.name = name
    if payload.brand is not None:
        s.brand = payload.brand.strip()
    if payload.purchased_date is not None:
        s.purchased_date = _parse_date(payload.purchased_date)
    if payload.initial_km is not None:
        s.initial_km = payload.initial_km or 0
    if payload.retired is not None:
        s.retired = 1 if payload.retired else 0
    if payload.note is not None:
        s.note = payload.note
    db.commit()
    db.refresh(s)
    return _shoe_out(s)


@router.post("/{shoe_id}/image")
async def upload_image(shoe_id: int, file: UploadFile = File(...),
                       db: Session = Depends(get_db)):
    s = db.get(Shoe, shoe_id)
    if not s:
        raise HTTPException(404, "跑鞋不存在")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "只支持图片文件")

    ext = Path(file.filename or "img.jpg").suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    dst = _IMG_DIR / f"shoe_{shoe_id}{ext}"
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    s.image_path = str(dst)
    db.commit()
    return {"ok": True, "has_image": True}


@router.get("/{shoe_id}/image")
def get_image(shoe_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse

    s = db.get(Shoe, shoe_id)
    if not s or not s.image_path:
        raise HTTPException(404, "无图片")
    p = Path(s.image_path)
    if not p.exists():
        raise HTTPException(404, "图片文件缺失")
    return FileResponse(p)


@router.post("/{shoe_id}/mileage")
def add_mileage(shoe_id: int, payload: MileageIn, db: Session = Depends(get_db)):
    s = db.get(Shoe, shoe_id)
    if not s:
        raise HTTPException(404, "跑鞋不存在")
    d = _parse_date(payload.run_date)

    # 同一天已有记录 → 累加（加法），否则新建一条
    row = (
        db.query(ShoeMileage)
        .filter(ShoeMileage.shoe_id == shoe_id, ShoeMileage.run_date == d)
        .first()
    )
    if row:
        row.km = round((row.km or 0) + payload.km, 2)
        if payload.note:
            row.note = payload.note
        db.commit()
        db.refresh(row)
        m = row
    else:
        m = ShoeMileage(shoe_id=shoe_id, run_date=d, km=payload.km, note=payload.note)
        db.add(m)
        db.commit()
        db.refresh(m)

    return {
        "id": m.id,
        "shoe_id": m.shoe_id,
        "run_date": m.run_date.isoformat(),
        "km": m.km,
        "note": m.note,
        "day_km": round(m.km, 2),                 # 当天该鞋累计
        "shoe_total_km": _shoe_out(s)["total_km"],
    }


@router.get("/{shoe_id}/mileage")
def list_mileage(shoe_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(ShoeMileage)
        .filter(ShoeMileage.shoe_id == shoe_id)
        .order_by(ShoeMileage.run_date.desc())
        .all()
    )
    return {
        "items": [
            {"id": m.id, "run_date": m.run_date.isoformat(), "km": m.km, "note": m.note}
            for m in rows
        ]
    }


@router.delete("/mileage/{mileage_id}")
def delete_mileage(mileage_id: int, db: Session = Depends(get_db)):
    m = db.get(ShoeMileage, mileage_id)
    if not m:
        raise HTTPException(404, "记录不存在")
    shoe_id = m.shoe_id
    db.delete(m)
    db.commit()
    s = db.get(Shoe, shoe_id)
    return {"ok": True, "shoe_total_km": _shoe_out(s)["total_km"] if s else 0}


@router.delete("/{shoe_id}")
def delete_shoe(shoe_id: int, db: Session = Depends(get_db)):
    s = db.get(Shoe, shoe_id)
    if not s:
        raise HTTPException(404, "跑鞋不存在")
    db.delete(s)
    db.commit()
    return {"ok": True, "name": s.name}
