"""灌入演示用的档案、体重与主观反馈，验证分析链路。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import requests

BASE = "http://127.0.0.1:8000"

PROFILE = {
    "name": "我",
    "gender": "male",
    "birth_year": 1995,
    "height_cm": 175,
    "max_hr": 195,
    "resting_hr": 58,
    "goal": "fat_loss",
    "target_weight_kg": 70,
    "weekly_available_days": 4,
    "experience": "beginner",
    "injury_notes": "右膝偶尔不适，避免连续两天高强度",
}

FEEDBACKS = [
    {"rpe": 6, "fatigue": 3, "soreness": 2, "mood": "还行", "pain": "右膝轻微不适", "note": "后半程有点顶"},
    {"rpe": 7, "fatigue": 3, "soreness": 3, "mood": "累", "pain": "", "note": ""},
    {"rpe": 5, "fatigue": 2, "soreness": 1, "mood": "轻松", "pain": "", "note": ""},
]


def main() -> None:
    r = requests.put(f"{BASE}/api/profile", json=PROFILE, timeout=30)
    print("档案写入:", r.status_code)

    today = date.today()
    for i in range(21, -1, -1):
        d = today - timedelta(days=i)
        weight = round(75.4 - (21 - i) * 0.11, 1)
        requests.post(
            f"{BASE}/api/profile/daily",
            json={
                "date": d.isoformat(),
                "weight_kg": weight,
                "sleep_h": round(6.5 + (i % 4) * 0.3, 1),
                "sleep_quality": 4,
                "resting_hr": 58 + (i % 3),
            },
            timeout=30,
        )
    print("体重与睡眠写入完成（22 天）")

    acts = requests.get(f"{BASE}/api/activities?limit=6", timeout=30).json()["items"]
    for a, fb in zip(acts[:3], FEEDBACKS):
        requests.post(f"{BASE}/api/activities/{a['id']}/feedback", json=fb, timeout=30)
    print("主观反馈写入:", len(acts[:3]), "条")

    print("\n=== 聚合指标 ===")
    print(json.dumps(requests.get(f"{BASE}/api/analytics/metrics", timeout=30).json(),
                     ensure_ascii=False, indent=2))

    print("\n=== 30 天汇总 ===")
    print(json.dumps(requests.get(f"{BASE}/api/analytics/summary?days=30", timeout=30).json(),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
