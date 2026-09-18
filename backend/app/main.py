from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routers import activities, agent, analytics, knowledge, places, profile, shoes, trace
from app.services import trace as trace_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="个人运动分析与训练助手", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(activities.router)
app.include_router(profile.feedback_router)
app.include_router(analytics.router)
app.include_router(profile.router)
app.include_router(agent.router)
app.include_router(knowledge.router)
app.include_router(places.router)
app.include_router(shoes.router)
app.include_router(trace.router)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """记录每个请求的路由派发与耗时，供前端终端面板展示执行链路。"""
    t0 = time.time()
    resp = await call_next(request)
    cost = (time.time() - t0) * 1000
    trace_svc.emit(
        "route",
        f"{request.method} {request.url.path}",
        f"{resp.status_code} · {cost:.0f}ms",
    )
    return resp
