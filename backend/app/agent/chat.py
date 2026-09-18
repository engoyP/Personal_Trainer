from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.agent import nodes
from app.agent.graph import _checkpointer
from app.services import llm, nutrition, trace

COACH_SYSTEM = """你是用户的私人跑步与体能教练，同时懂运动营养，也是他的数据分析师。
你手上有已经算好的指标（含营养目标，以及最近一次生成的饮食计划）。硬性规则：
- 只解读已给出的数字，绝不编造，也不要自己重新计算。
- 回答要具体可执行，说清理由，不要泛泛而谈。
- 涉及伤病或持续疼痛，明确建议停跑或就医，不要鼓励硬撑。
- 指标里若标注了「估算」或存在缺失项（常见的是原始文件不含心率，导致训练负荷为配速估算值、
  心率区间为空），不要当成精确实测值下结论；据此给强度或伤病建议时要留有余地，并说明依据不充分。
- **食材替换**：用户说「没有XX / 想换掉XX」时，按营养等价给替代方案（热量与主要营养素相近、
  优先同类食材），说清为什么能换。知识库里有营养依据就引用；拿不准是否等价时明说不确定在哪，
  不要硬凑。用户提到买不到/不吃的食材，后续建议里一并避开。
- 中文回答，语气像站在旁边的教练，直接、不说废话。
- 除非用户要求详细说明，控制在 300 字以内。"""


class ChatState(TypedDict, total=False):
    request: str
    as_of: str
    profile: dict
    metrics: dict
    recent_activities: list
    nutrition_target: dict            # 每日营养目标（代码算）
    latest_diet: dict                 # 最近一次生成的饮食计划
    evidence: list[dict]
    messages: Annotated[list[Any], add_messages]


def _last_user_message(state: dict) -> str:
    """取用户最后一句话当检索查询。

    用单轮问题而不是整个上下文去检索：上下文里混着教练上一轮的长回复，
    拿它当 query 会把检索带偏到上一轮的话题上。
    """
    for msg in reversed(state.get("messages") or []):
        role, content = nodes.role_content(msg)
        if role == "user" and content:
            return content
    return ""


# 食材替换类提问的信号词
_SWAP_HINTS = ("没有", "换成", "替换", "替代", "换一个", "换别的", "不爱吃",
               "买不到", "不吃", "过敏", "家里没", "手边没")


def _build_query(msg: str) -> str:
    """食材替换类提问时给检索 query 补营养词。

    不加的话，语义检索容易被「训练」类文章带偏（用户问题里没几个营养词），
    捞回来一堆训练文章，而真正该用的「食材替换/营养替代」条目排不进 top。
    """
    if any(k in msg for k in _SWAP_HINTS):
        return f"{msg} 食材替换 食物替代 同等营养 热量相近"
    return msg


def build_context(m: dict, p: dict, recent: list, evidence_text: str,
                  nutrition_target: dict | None = None,
                  latest_diet: dict | None = None) -> str:
    """拼给教练看的上下文。

    抽成独立函数是为了能脱离 LLM 单独回归：数据质量那句话必须真的出现在
    上下文里，不能只在代码里"算出来了"就算数 —— 指标是整包 JSON 丢过去的，
    模型光看到 `estimated_load_ratio_28d: 1.0` 这种键名并不知道该收敛结论。
    """
    blocks = [
        f"【用户档案】{json.dumps(p, ensure_ascii=False)}",
        f"【聚合指标】{json.dumps(m, ensure_ascii=False)}",
        f"【最近运动】{json.dumps(recent, ensure_ascii=False)}",
    ]
    if nutrition_target and nutrition_target.get("available"):
        blocks.append(f"【营养目标】{nutrition.render_targets(nutrition_target)}")
    if latest_diet:
        # 只给原则和示例餐，数字在上面【营养目标】里已经有了（避免两份数字打架）
        slim = {k: v for k, v in latest_diet.items()
                if k in ("principles", "training_day_example", "rest_day_example")}
        if slim:
            blocks.append(f"【最新饮食计划】{json.dumps(slim, ensure_ascii=False)}")
    quality = nodes._data_quality_line(m)
    if quality:
        # _data_quality_line 自带 "- 数据质量：" 前缀（评估提示词里是列表项），
        # 这里已经有【数据质量】标题了，剥掉免得连着念两遍。
        body = quality.lstrip("- ").removeprefix("数据质量：")
        blocks.append(f"【数据质量】{body}")
    return "\n".join(blocks) + f"\n\n【循证依据·来自训练知识库】\n{evidence_text}"


def respond(state: dict) -> dict:
    m = state.get("metrics") or {}
    p = state.get("profile") or {}
    recent = state.get("recent_activities") or []

    query = _build_query(_last_user_message(state) or state.get("request") or "")
    evidence_text, evidence = nodes.evidence_for(query, top_n=4)

    context = build_context(m, p, recent, evidence_text,
                            nutrition_target=state.get("nutrition_target"),
                            latest_diet=state.get("latest_diet"))

    reply = nodes.llm_chat(COACH_SYSTEM, context, state.get("messages") or [])
    return {"messages": [{"role": "assistant", "content": reply}],
            "evidence": evidence}


def build_chat_graph():
    g = StateGraph(ChatState)
    t = trace.node
    g.add_node("load_context", t("拉数据算指标")(nodes.load_context))
    g.add_node("respond", t("生成回答(LLM)")(respond))
    g.set_entry_point("load_context")
    g.add_edge("load_context", "respond")
    g.add_edge("respond", END)

    ck = _checkpointer()
    return g.compile(checkpointer=ck) if ck else g.compile()


_chat_graph = None


def get_chat_graph():
    global _chat_graph
    if _chat_graph is None:
        _chat_graph = build_chat_graph()
    return _chat_graph


def chat(message: str, thread_id: str | None = None, as_of: str | None = None) -> dict:
    g = get_chat_graph()
    tid = thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": tid}}

    g.invoke(
        {
            "request": message,
            "as_of": as_of or date.today().isoformat(),
            "messages": [{"role": "user", "content": message}],
        },
        config,
    )

    snapshot = g.get_state(config)
    vals = snapshot.values
    msgs = vals.get("messages") or []

    reply = ""
    for m in reversed(msgs):
        role, content = nodes.role_content(m)
        if role == "assistant" and content:
            reply = content
            break

    # 只回传出处本身，不回传喂给模型的格式（前端要展示的是可点的链接）
    sources = [
        {"id": a.get("id"), "title": a.get("title"), "organization": a.get("organization"),
         "url": a.get("url"), "published_at": a.get("published_at"),
         "evidence_level": a.get("evidence_level")}
        for a in (vals.get("evidence") or [])
    ]

    return {"thread_id": tid, "reply": reply, "sources": sources}


def chat_stream(message: str, thread_id: str | None = None, as_of: str | None = None):
    """流式对话：先拉数据/检索，再流式调 LLM，边生成边产出。

    yield 的事件：{"type":"thread_id","thread_id":...} / {"type":"delta","text":...}
               / {"type":"sources","sources":[...]} / {"type":"error","error":...}
    """
    g = get_chat_graph()
    tid = thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": tid}}

    # 读历史（checkpoint 里存的多轮消息）
    hist: list = []
    try:
        snap = g.get_state(config)
        hist = snap.values.get("messages") or []
    except Exception:
        hist = []

    # 1. 拉数据算指标（确定性节点，快）
    ctx = nodes.load_context({
        "as_of": as_of or date.today().isoformat(),
        "request": message,
    })
    metrics = ctx["metrics"]
    profile = ctx["profile"]
    recent = ctx["recent_activities"]

    # 2. 检索知识库
    evidence_text, evidence = nodes.evidence_for(_build_query(message), top_n=4)
    context = build_context(metrics, profile, recent, evidence_text,
                            nutrition_target=ctx.get("nutrition_target"),
                            latest_diet=ctx.get("latest_diet"))

    # 3. 拼消息（历史 + 新用户问题）
    msgs: list[dict] = [{"role": "system", "content": f"{COACH_SYSTEM}\n\n{context}"}]
    for h in hist[-12:]:
        role, content = nodes.role_content(h)
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": message})

    yield {"type": "thread_id", "thread_id": tid}

    # 4. 流式 LLM
    full = ""
    try:
        for delta in llm.chat_completion_stream(msgs, temperature=0.5):
            full += delta
            yield {"type": "delta", "text": delta}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "error": str(e)}
        return

    sources = [
        {"id": a.get("id"), "title": a.get("title"), "organization": a.get("organization"),
         "url": a.get("url"), "published_at": a.get("published_at"),
         "evidence_level": a.get("evidence_level")}
        for a in evidence
    ]
    yield {"type": "sources", "sources": sources}

    # 5. 把本轮对话写回 checkpoint（保持多轮记忆）
    try:
        from langchain_core.messages import AIMessage, HumanMessage
        g.update_state(config, {
            "messages": [HumanMessage(content=message), AIMessage(content=full)]
        })
    except Exception:
        pass
