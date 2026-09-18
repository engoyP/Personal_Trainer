from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.agent.chat import chat as chat_with_coach
from app.agent.chat import chat_stream
from app.agent.graph import confirm_plan, start_plan
from app.database import get_db
from app.models.fitness import TrainingPlan

router = APIRouter(prefix="/api/agent", tags=["agent"])


class PlanRequest(BaseModel):
    as_of: str | None = None
    request: str = ""
    feedback: str = ""


class ConfirmRequest(BaseModel):
    thread_id: str


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    as_of: str | None = None


@router.post("/chat")
def chat_endpoint(payload: ChatRequest):
    """多轮教练对话，基于聚合指标回答，看不到原始采样点。"""
    try:
        return chat_with_coach(payload.message, payload.thread_id, payload.as_of)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"对话失败：{e}")


@router.post("/chat/stream")
def chat_stream_endpoint(payload: ChatRequest):
    """流式教练对话：SSE，边生成边推文本片段。"""
    def gen():
        try:
            for ev in chat_stream(payload.message, payload.thread_id, payload.as_of):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/plan/generate")
def generate(payload: PlanRequest):
    """跑图到 finalize 前暂停，返回草稿等人工确认。"""
    try:
        return start_plan(payload.as_of, payload.request, payload.feedback)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成计划失败：{e}")


@router.post("/plan/confirm")
def confirm(payload: ConfirmRequest):
    try:
        return confirm_plan(payload.thread_id)
    except Exception as e:
        raise HTTPException(500, f"确认失败：{e}")


@router.get("/plans")
def list_plans(limit: int = 10, db: Session = Depends(get_db)):
    rows = db.query(TrainingPlan).order_by(desc(TrainingPlan.created_at)).limit(limit).all()
    return {
        "items": [
            {
                "id": r.id,
                "week_start": r.week_start.isoformat() if r.week_start else None,
                "status": r.status,
                "plan": r.plan_json,
                "explanation": r.explanation,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    r = db.get(TrainingPlan, plan_id)
    if not r:
        raise HTTPException(404, "计划不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}
