from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from app.config import settings
from app.database import SessionLocal
from app.models.fitness import Profile, TrainingPlan
from app.services import analytics, llm, nutrition, trace


# ---------------------------------------------------------------- LLM 调用
def _llm_json(system: str, user: str) -> dict:
    """调用 DeepSeek（OpenAI 兼容）并强制 JSON 输出。"""
    return llm.json_completion(system, user)


def role_content(m) -> tuple[str, str]:
    """统一取消息角色与内容。

    LangGraph 的 add_messages 会把 dict 转成 LangChain 消息对象，
    而 HumanMessage / AIMessage 没有 role 属性，只有 type（human / ai）。
    两种形态都要支持，否则角色取到 None，历史和用户问题会被静默丢弃。
    """
    if isinstance(m, dict):
        return m.get("role") or "", m.get("content") or ""

    role = getattr(m, "role", None)
    if role not in ("user", "assistant", "system"):
        role = {"human": "user", "ai": "assistant"}.get(getattr(m, "type", ""), "")
    return role or "", getattr(m, "content", "") or ""


def llm_chat(system: str, context: str, history: list) -> str:
    """多轮对话，返回自然语言。history 里只取最近若干轮控制 token。"""
    msgs: list[dict] = [{"role": "system", "content": f"{system}\n\n{context}"}]
    for h in history[-12:]:
        role, content = role_content(h)
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})

    if len(msgs) == 1:
        raise RuntimeError("没有可发送的用户消息")

    return llm.chat_completion(msgs, temperature=0.5)


def _fmt_pace(sec) -> str:
    if not sec:
        return "无"
    m, s = divmod(int(sec), 60)
    return f"{m}'{s:02d}\"/km"


# ---------------------------------------------------------------- 知识库检索
def evidence_for(query: str, top_n: int = 5) -> tuple[str, list[dict]]:
    """从训练知识库捞相关条目，渲染成可注入 prompt 的文本。

    失败一律降级为空依据：知识库没建起来、表不存在、库里还没数据，
    都不应该让计划生成直接挂掉。基础专业能力模型自己就有，
    知识库只是加分项，不是前置依赖。
    """
    # 检索 query 截到 120 字：query 越长，rerank 每对 padding 越长，耗时翻几倍
    # （实测 12 字 13s → 174 字 59s）。120 字在速度和检索准度之间取平衡，
    # 太短（60 字）会砍掉风险点/约束等关键检索词导致命中骤降。
    query = (query or "").strip()[:120]
    try:
        from app.knowledge.retrieve import EVIDENCE_RULES, format_evidence, retrieve

        db = SessionLocal()
        try:
            arts = retrieve(db, query, top_n=top_n)
        finally:
            db.close()

        trace.emit("tool", "知识库检索", f"命中 {len(arts)} 条 · {query[:30]}…")
        if not arts:
            return "（知识库暂无相关条目）", []

        return f"{format_evidence(arts)}\n\n{EVIDENCE_RULES}", arts
    except Exception as e:  # noqa: BLE001
        trace.emit("tool", "知识库检索", f"降级：{type(e).__name__}")
        return f"（知识库不可用：{type(e).__name__}）", []


# ---------------------------------------------------------------- 节点 1
def load_context(state: dict) -> dict:
    """确定性节点：拉数据 + 算指标。"""
    db = SessionLocal()
    try:
        as_of = date.fromisoformat(state["as_of"]) if state.get("as_of") else date.today()
        p: Profile | None = db.query(Profile).first()

        age = (date.today().year - p.birth_year) if (p and p.birth_year) else 30
        profile = {
            "age": age,
            "gender": (p.gender if p else "male") or "male",
            "height_cm": p.height_cm if p else None,
            "weight_kg": None,
            "max_hr": (p.max_hr if p and p.max_hr else None) or settings.DEFAULT_MAX_HR,
            "resting_hr": (p.resting_hr if p and p.resting_hr else None) or settings.DEFAULT_RESTING_HR,
            "goal": (p.goal if p else "health") or "health",
            "target_weight_kg": p.target_weight_kg if p else None,
            "weekly_available_days": (p.weekly_available_days if p else 4) or 4,
            "experience": (p.experience if p else "beginner") or "beginner",
            "injury_notes": (p.injury_notes if p else "") or "",
        }

        from app.models.fitness import DailyMetric

        latest = db.query(DailyMetric).order_by(DailyMetric.date.desc()).first()
        profile["weight_kg"] = latest.weight_kg if latest else None

        metrics = analytics.build_metrics(db, as_of)

        from app.models.fitness import Activity

        acts = (
            db.query(Activity)
            .filter(Activity.start_time >= datetime.combine(as_of - timedelta(days=28), datetime.min.time()))
            .order_by(Activity.start_time.desc())
            .limit(10)
            .all()
        )

        # 最新饮食计划：让教练能回答「计划里的西兰花能不能换」这种追问
        latest_plan = (
            db.query(TrainingPlan)
            .order_by(TrainingPlan.created_at.desc())
            .first()
        )
        latest_diet = None
        if latest_plan and isinstance(latest_plan.plan_json, dict):
            latest_diet = latest_plan.plan_json.get("nutrition")

        return {
            "profile": profile,
            "metrics": metrics,
            "recent_activities": analytics.activity_cards(acts),
            "nutrition_target": nutrition.compute_targets(profile, metrics),
            "latest_diet": latest_diet,
        }
    finally:
        db.close()


# ---------------------------------------------------------------- 节点 2
ASSESS_SYSTEM = """你是资深跑步与体能教练。你会收到一份已经计算完成的运动数据指标。
硬性要求：
- 禁止编造、修改或重新计算任何数字，只能解读已给出的数字。
- 不要输出计算公式。
- 指标里标注为「估算」或缺失的项目（常见的是心率缺失导致训练负荷为估算值），
  不要当作精确实测值下定论；据此给风险结论时要留有余地，并说明依据不充分。
- 只输出 JSON，不要输出任何其他文字。

输出格式：
{
  "assessment": "150 字以内，说明当前体能状态、疲劳程度、是否在进步",
  "risk_flags": ["风险点，如：急慢性负荷比偏高"],
  "constraints": ["本周必须遵守的具体约束，如：跑步不超过 3 次；单次不超过 45 分钟"]
}"""

PLAN_SYSTEM = """你是资深跑步与体能教练，同时懂运动营养。根据评估结果、约束和用户目标，生成一周训练计划 + 饮食安排。
硬性要求：
- 只输出 JSON，不要输出任何解释文字或计算过程。
- days 必须覆盖完整的 7 天，每天一条。
- type 只能是：rest / easy / tempo / interval / long / strength / cross
- rest 日的 duration_min 填 0，distance_km 填 0。
- nutrition 里的热量与三大营养素数字**由系统给定，你必须原样使用，禁止改动、重算或新编数字**；
  你只负责给出可执行的原则和三餐示例。
- 饮食建议要具体到能落地（食材 + 份量感），不要说「多吃蛋白质」这种空话。

输出格式：
{
  "week_start": "YYYY-MM-DD",
  "focus": "本周训练重点，一句话",
  "days": [
    {"date": "YYYY-MM-DD", "type": "easy", "duration_min": 40, "distance_km": 7,
     "target_hr_zone": "z2", "description": "轻松跑，全程能用鼻子呼吸"}
  ],
  "total_distance_km": 30,
  "nutrition": {
    "principles": ["每餐先吃蛋白和蔬菜再吃主食", "训练日把碳水前移到跑前2小时", "..."],
    "training_day_example": {"breakfast": "燕麦50g+鸡蛋2个+牛奶250ml", "lunch": "...", "dinner": "...", "snack": "..."},
    "rest_day_example": {"breakfast": "...", "lunch": "...", "dinner": "...", "snack": "..."}
  }
}"""


def _data_quality_line(m: dict) -> str:
    """心率缺失 / 负荷是估算的，必须让教练知道。

    华为 GPX/TCX 导出的活动没有心率字段，训练负荷是按配速估的。
    不说明的话，教练会把估算出来的 ATL/CTL/急慢性比当成精确实测值，
    据此判「受伤风险高、必须减量」—— 而计划校验里 acwr > 1.3 是**硬拦截**。
    """
    miss, est = m.get("hr_missing_ratio_28d"), m.get("estimated_load_ratio_28d")
    parts = []
    if miss:
        parts.append(f"近 28 天 {round(miss * 100)}% 的运动没有心率数据")
    if est:
        parts.append(
            f"{round(est * 100)}% 的训练负荷是按配速估算的"
            "（原始文件不含心率，如华为 GPX/TCX 导出）"
        )
    if not parts:
        return ""
    return ("- 数据质量：" + "，".join(parts)
            + "。基于负荷的判断（受伤风险、是否需要减量）请留有余地，别当成精确实测值。")


def assess(state: dict) -> dict:
    m = state.get("metrics") or {}
    p = state.get("profile") or {}
    recent = state.get("recent_activities") or []
    quality = _data_quality_line(m)

    user = f"""个人档案：{json.dumps(p, ensure_ascii=False)}

近期指标（已算好，只解读不要重算）：
- 慢性负荷 CTL {m.get('ctl')}，急性负荷 ATL {m.get('atl')}，状态 TSB {m.get('tsb')}，急慢性比 {m.get('acwr')}
- 近 8 周周跑量(km)：{m.get('weekly_distance_km')}
- 近 8 周周负荷：{m.get('weekly_load')}
- 平均配速：{_fmt_pace(m.get('avg_pace_s_per_km'))}，配速变化 {m.get('pace_trend_pct')}%（负数为变快）
- 平均心率 {m.get('avg_hr') or '无心率数据'}，静息心率周趋势 {m.get('resting_hr_trend')} bpm/周
- 平均步频 {m.get('avg_cadence_spm') or '无'} spm，平均步幅 {m.get('avg_stride_m') or '无'} m
- 体重周变化 {m.get('weight_trend_kg_per_week')} kg/周，平均睡眠 {m.get('sleep_avg_h') or '无'} 小时
- 平均 RPE {m.get('avg_rpe') or '无'}，近 28 天运动 {m.get('activity_count_28d')} 次
- 疼痛记录：{m.get('pain_flags') or '无'}
{quality}

最近运动：{json.dumps(recent, ensure_ascii=False)}

用户要求：{state.get('request') or '无特殊要求'}"""

    out = _llm_json(ASSESS_SYSTEM, user)
    return {
        "assessment": out.get("assessment", ""),
        "risk_flags": out.get("risk_flags") or [],
        "constraints": out.get("constraints") or [],
    }


# ---------------------------------------------------------------- 节点 3
def _apply_nutrition(plan_obj: dict, nt: dict) -> dict:
    """把代码算好的营养数字写回计划，防止 LLM 自造热量/克数。

    plan 和 revise 两个节点都要调 —— revise 是 LLM 重新生成，营养数字不会原样带回来。
    """
    plan_obj["nutrition"] = dict(plan_obj.get("nutrition") or {})
    if nt.get("available"):
        plan_obj["nutrition"].update({
            "target_kcal": nt["target_kcal"], "protein_g": nt["protein_g"],
            "fat_g": nt["fat_g"], "carb_g": nt["carb_g"],
            "deficit_kcal": nt.get("deficit_kcal", 0), "note": nt.get("note", ""),
        })
    return plan_obj


def plan(state: dict) -> dict:
    m = state.get("metrics") or {}
    p = state.get("profile") or {}
    nt = state.get("nutrition_target") or {}

    # 检索查询刻意用「目标 + 风险点 + 用户诉求」拼，而不是把整个档案丢进去。
    # 查询越具体，捞出来的文章越贴题 —— 用全文当 query 等于没筛。
    # 减脂目标额外带上饮食词，否则检索全被训练类文章占满、捞不到营养知识。
    goal = str(p.get("goal") or "")
    diet_hint = "减脂饮食 营养搭配 食材热量" if goal.lower() in ("fat_loss", "weight_loss") else ""
    query = " ".join(filter(None, [
        goal,
        diet_hint,
        " ".join(state.get("risk_flags") or []),
        " ".join(state.get("constraints") or []),
        str(state.get("request") or ""),
        str(state.get("assessment") or "")[:200],
    ]))
    evidence_text, evidence = evidence_for(query or "跑步训练 计划 心率区间", top_n=5)

    user = f"""目标：{p.get('goal')}；经验：{p.get('experience')}；每周可训练 {p.get('weekly_available_days')} 天
伤病史：{p.get('injury_notes') or '无'}
计划起始日 week_start 必须是：{state.get('as_of')}

当前状态评估：{state.get('assessment')}
风险点：{state.get('risk_flags')}
必须遵守的约束：{state.get('constraints')}

参考指标：
- 近 4 周周跑量：{m.get('weekly_distance_km', [])[-4:]}，近 8 周：{m.get('weekly_distance_km')}
- 平均配速 {_fmt_pace(m.get('avg_pace_s_per_km'))}，历史单次最长 {m.get('longest_recent_min')} 分钟
- TSB {m.get('tsb')}，急慢性比 {m.get('acwr')}
- 体重 {p.get('weight_kg')} kg，目标体重 {p.get('target_weight_kg')} kg

【营养目标（系统算好，原样使用，禁止改动）】
{nutrition.render_targets(nt)}

循证依据（训练知识库检索结果）：
{evidence_text}

用户额外要求：{state.get('request') or '无'}
{f'用户的修改意见：{state.get("feedback")}' if state.get('feedback') else ''}

请生成训练计划 + 饮食安排。"""

    out = _llm_json(PLAN_SYSTEM, user)
    days = out.get("days") or []
    if not days:
        raise RuntimeError("模型没有返回计划内容")

    # 营养数字由代码兜底写回，防止模型在 nutrition 里自造热量/克数
    _apply_nutrition(out, nt)

    return {"draft_plan": out, "evidence": evidence}


# ---------------------------------------------------------------- 节点 4
def validate(state: dict) -> dict:
    """确定性规则引擎。约束不靠 prompt 保证，靠代码兜底。"""
    plan_obj = state.get("draft_plan") or {}
    m = state.get("metrics") or {}
    p = state.get("profile") or {}
    days = plan_obj.get("days") or []

    errors: list[str] = []
    warnings: list[str] = []

    if len(days) != 7:
        warnings.append(f"计划共 {len(days)} 天，建议覆盖完整 7 天")

    if not any(d.get("type") == "rest" for d in days):
        errors.append("计划里没有完全休息日，必须至少安排 1 天 rest")

    total_km = sum(d.get("distance_km") or 0 for d in days)
    recent = (m.get("weekly_distance_km") or [])[-4:]
    base = sum(recent) / len(recent) if recent else 0.0
    if base > 1 and total_km > base * 1.10:
        errors.append(
            f"计划周跑量 {total_km:.1f}km 超过近 4 周均值 {base:.1f}km 的 10%，需降到 {base * 1.10:.1f}km 以内"
        )

    total_min = sum(d.get("duration_min") or 0 for d in days)
    hard_min = sum(d.get("duration_min") or 0 for d in days
                   if d.get("type") in ("interval", "tempo"))
    if total_min and hard_min / total_min > 0.20:
        errors.append(
            f"高强度时长占比 {hard_min / total_min * 100:.0f}%，超过 20% 上限，需下调"
        )

    # 负荷可能有一部分是配速估算的（无心率文件）。安全闸门**不因此放松** ——
    # 放松等于拿不确定性去冒受伤的风险 —— 但必须把话说清楚，
    # 让人知道这条拦截的分量有多重，而不是看起来像精确实测。
    est_ratio = m.get("estimated_load_ratio_28d") or 0
    est_note = (
        f"（近 28 天 {round(est_ratio * 100)}% 负荷为无心率估算值，拦截力度请自行权衡）"
        if est_ratio >= 0.3 else ""
    )

    acwr = m.get("acwr") or 0
    if acwr > 1.3:
        warnings.append(f"急慢性负荷比 {acwr} 偏高（>1.3），本周应减量{est_note}")
        if total_km > base:
            errors.append(
                f"负荷比 {acwr} 超过 1.3，本周总跑量必须低于近 4 周均值 {base:.1f}km{est_note}"
            )

    if (m.get("tsb") or 0) < -20:
        if any(d.get("type") in ("interval", "tempo", "long") for d in days):
            errors.append(f"疲劳指数 TSB < -20，本周禁止安排 interval / tempo / long{est_note}")

    longest = m.get("longest_recent_min") or 0
    if longest and any((d.get("duration_min") or 0) > longest * 1.10 for d in days):
        errors.append(f"单次时长需控制在 {longest * 1.10:.0f} 分钟内（历史最长 {longest:.0f} 分钟）")

    if p.get("goal") == "fat_loss":
        wt = m.get("weight_trend_kg_per_week") or 0
        if wt < -1.0:
            errors.append(f"近期体重下降 {abs(wt):.1f}kg/周过快，减脂速率应控制在 0.5–1.0kg/周")

    if m.get("pain_flags"):
        if any(d.get("type") in ("interval", "tempo", "long") for d in days):
            warnings.append("近期有疼痛记录：" + "、".join(m["pain_flags"]) + "，强度课需谨慎")

    return {"validation": {"passed": not errors, "errors": errors, "warnings": warnings}}


# ---------------------------------------------------------------- 节点 5
REVISE_SYSTEM = """你是资深跑步与体能教练。上一版训练计划没有通过安全校验，请按错误逐条修正。
硬性要求：只输出 JSON，结构与上一版完全一致（week_start / focus / days / total_distance_km / nutrition），days 仍为 7 天。
只修改被指出问题的部分，不要整体推翻重写。**nutrition 部分原样保留，不要改动营养数字。**"""

MAX_REVISION = 2


def revise(state: dict) -> dict:
    v = state.get("validation") or {}
    user = f"""校验错误：{json.dumps(v.get('errors'), ensure_ascii=False)}
提醒：{json.dumps(v.get('warnings'), ensure_ascii=False)}

上一版计划：{json.dumps(state.get('draft_plan'), ensure_ascii=False)}

约束：{json.dumps(state.get('constraints'), ensure_ascii=False)}
请输出修正后的完整计划 JSON。"""

    out = _llm_json(REVISE_SYSTEM, user)
    if not out.get("days"):
        raise RuntimeError("模型没有返回修正后的计划")

    _apply_nutrition(out, state.get("nutrition_target") or {})

    return {
        "draft_plan": out,
        "revision_count": (state.get("revision_count") or 0) + 1,
    }


# ---------------------------------------------------------------- 节点 6
def finalize(state: dict) -> dict:
    plan_obj = state.get("draft_plan") or {}
    v = state.get("validation") or {}

    explanation = "\n".join(
        filter(None, [
            state.get("assessment", ""),
            f"本周重点：{plan_obj.get('focus', '')}",
            f"安全校验：{'通过' if v.get('passed') else '经 ' + str(state.get('revision_count', 0)) + ' 轮修正后通过'}",
        ])
    )

    db = SessionLocal()
    try:
        ws = plan_obj.get("week_start")
        rec = TrainingPlan(
            week_start=date.fromisoformat(ws) if ws else date.today(),
            status="confirmed" if state.get("human_approved") else "pending",
            plan_json=plan_obj,
            explanation=explanation,
            metrics_json=state.get("metrics"),
            evidence_json=state.get("evidence") or [],
        )
        db.add(rec)
        db.commit()
        plan_id = rec.id
    finally:
        db.close()

    return {"final_plan": plan_obj, "explanation": explanation, "plan_id": plan_id}
