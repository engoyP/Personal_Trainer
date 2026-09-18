"""端到端验证 LangGraph：生成 -> 暂停等待确认 -> 确认落库。"""
from __future__ import annotations

import json

import requests

BASE = "http://127.0.0.1:8000"
REQUEST = "我想减脂，每周能练 4 天，右膝不太好别安排太多强度"


def main() -> None:
    r = requests.post(
        f"{BASE}/api/agent/plan/generate",
        json={"request": REQUEST},
        timeout=180,
    )
    print("HTTP", r.status_code)
    d = r.json()

    if r.status_code != 200:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return

    print("thread_id:", d.get("thread_id"))
    print("暂停在节点:", d.get("awaiting"))
    print("\n=== 状态评估 ===")
    print(d.get("assessment"))
    print("\n风险点:", d.get("risk_flags"))
    print("约束:", d.get("constraints"))

    v = d.get("validation") or {}
    print("\n=== 安全校验 ===")
    print("通过:", v.get("passed"))
    if v.get("errors"):
        print("错误:", *v["errors"], sep="\n  - ")
    if v.get("warnings"):
        print("警告:", *v["warnings"], sep="\n  - ")
    print("修正轮数:", d.get("revision_count"))

    p = d.get("draft_plan") or {}
    print(f"\n=== 计划 {p.get('week_start')} | {p.get('focus')} ===")
    for day in p.get("days", []):
        print(f"  {day.get('date')}  {str(day.get('type')):9s} "
              f"{day.get('duration_min')}分 {day.get('distance_km')}km "
              f"{day.get('target_hr_zone')}  {day.get('description')}")
    print("总跑量:", p.get("total_distance_km"), "km")

    print("\n=== 模拟人工确认 ===")
    r2 = requests.post(
        f"{BASE}/api/agent/plan/confirm",
        json={"thread_id": d["thread_id"]},
        timeout=60,
    )
    print("HTTP", r2.status_code)
    c = r2.json()
    print("plan_id:", c.get("plan_id"), " status:", c.get("status"))
    print("说明:", c.get("explanation"))

    r3 = requests.get(f"{BASE}/api/agent/plans?limit=3", timeout=30)
    print("\n已保存计划数:", len(r3.json().get("items", [])))


if __name__ == "__main__":
    main()
