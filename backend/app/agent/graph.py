from __future__ import annotations

import uuid
from datetime import date

from langgraph.graph import END, StateGraph

from app.agent import nodes
from app.agent.state import PlanState
from app.config import settings
from app.services import trace


_ck = None
_ck_ready = False


def _checkpointer():
    """优先用 SQLite 持久化，装不了就退化为内存。全局只建一次。"""
    global _ck, _ck_ready
    if _ck_ready:
        return _ck

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        _ck = SqliteSaver.from_conn_string(settings.CHECKPOINT_DB)
    except Exception:
        try:
            from langgraph.checkpoint.memory import MemorySaver

            _ck = MemorySaver()
        except Exception:
            _ck = None

    _ck_ready = True
    return _ck


def route_after_validate(state: dict) -> str:
    v = state.get("validation") or {}
    if v.get("passed"):
        return "finalize"
    if (state.get("revision_count") or 0) >= nodes.MAX_REVISION:
        return "finalize"
    return "revise"


def build_graph():
    g = StateGraph(PlanState)
    t = trace.node  # 节点埋点装饰器
    g.add_node("load_context", t("1. 拉数据算指标")(nodes.load_context))
    g.add_node("assess", t("2. 评估体能状态(LLM)")(nodes.assess))
    g.add_node("plan", t("3. 检索知识库 + 生成计划(LLM)")(nodes.plan))
    g.add_node("validate", t("4. 安全规则校验")(nodes.validate))
    g.add_node("revise", t("5. 按校验意见修正(LLM)")(nodes.revise))
    g.add_node("finalize", t("6. 定稿入库")(nodes.finalize))

    g.set_entry_point("load_context")
    g.add_edge("load_context", "assess")
    g.add_edge("assess", "plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges(
        "validate",
        route_after_validate,
        {"finalize": "finalize", "revise": "revise"},
    )
    g.add_edge("revise", "validate")
    g.add_edge("finalize", END)

    ck = _checkpointer()
    return g.compile(interrupt_before=["finalize"], checkpointer=ck) if ck else g.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def start_plan(as_of: str | None = None, request: str = "", feedback: str = "") -> dict:
    """跑图到 finalize 前暂停，把草稿交给人确认。"""
    g = get_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    g.invoke(
        {
            "request": request,
            "feedback": feedback,
            "horizon_days": 7,
            "as_of": as_of or date.today().isoformat(),
            "revision_count": 0,
            "human_approved": False,
        },
        config,
    )

    snapshot = g.get_state(config)
    v = snapshot.values
    return {
        "thread_id": thread_id,
        "assessment": v.get("assessment"),
        "risk_flags": v.get("risk_flags") or [],
        "constraints": v.get("constraints") or [],
        "draft_plan": v.get("draft_plan"),
        "validation": v.get("validation"),
        "metrics": v.get("metrics"),
        "evidence": v.get("evidence") or [],
        "revision_count": v.get("revision_count", 0),
        "awaiting": list(snapshot.next or []),
    }


def confirm_plan(thread_id: str) -> dict:
    """人确认后放行 finalize。"""
    g = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    g.update_state(config, {"human_approved": True})
    g.invoke(None, config)

    snapshot = g.get_state(config)
    v = snapshot.values
    return {
        "plan_id": v.get("plan_id"),
        "final_plan": v.get("final_plan"),
        "explanation": v.get("explanation"),
        "status": "confirmed",
    }
