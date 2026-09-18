"""执行链路追踪的查询接口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.services.trace import get_bus

router = APIRouter(prefix="/api/trace", tags=["trace"])


@router.get("/recent")
def recent(since_id: int = 0, tail: int = 200):
    """增量拉取事件：since_id 之后的新事件。前端轮询用。"""
    bus = get_bus()
    if since_id <= 0:
        return {"since_id": bus.counter, "events": bus.tail(tail)}
    return {"since_id": bus.counter, "events": bus.since(since_id)}
