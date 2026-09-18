from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


class AthleteProfile(TypedDict, total=False):
    """长期档案，DB 持久化，作为图的输入而非运行时状态。"""
    age: int
    gender: str
    height_cm: float
    weight_kg: float
    max_hr: int
    resting_hr: int
    goal: str                      # fat_loss / endurance / speed / health
    target_weight_kg: float | None
    weekly_available_days: int
    experience: str
    injury_notes: str


class ComputedMetrics(TypedDict, total=False):
    """分析引擎产出，纯代码算好，LLM 只读不准改。"""
    ctl: float
    atl: float
    tsb: float
    acwr: float
    weekly_distance_km: list[float]
    weekly_load: list[float]
    avg_pace_s_per_km: float | None
    pace_trend_pct: float
    avg_hr: float | None
    avg_cadence_spm: float | None
    avg_stride_m: float | None
    weight_trend_kg_per_week: float
    sleep_avg_h: float | None
    resting_hr_trend: float
    avg_rpe: float | None
    pain_flags: list[str]
    activity_count_28d: int
    longest_recent_min: float


class PlanDay(TypedDict, total=False):
    date: str
    type: str                      # rest / easy / tempo / interval / long / strength / cross
    duration_min: int
    distance_km: float | None
    target_hr_zone: str | None
    description: str


class Plan(TypedDict, total=False):
    week_start: str
    days: list[PlanDay]
    total_distance_km: float
    focus: str


class PlanState(TypedDict, total=False):
    # 输入
    request: str
    feedback: str
    horizon_days: int
    as_of: str

    # load_context 填充
    profile: AthleteProfile
    metrics: ComputedMetrics
    recent_activities: list[dict]
    nutrition_target: dict            # 每日热量与三大营养素目标（纯代码算）

    # assess 填充
    assessment: str
    risk_flags: list[str]
    constraints: list[str]

    # plan 填充
    draft_plan: Plan | None
    evidence: list[dict]             # 知识库里检索到的循证依据（摘要+要点，非全文）

    # validate 填充
    validation: dict | None        # {"passed": bool, "errors": [...], "warnings": [...]}
    revision_count: int

    # 人工确认
    human_approved: bool

    # finalize 填充
    final_plan: Plan | None
    explanation: str
    plan_id: int | None

    messages: Annotated[list[Any], add_messages]
