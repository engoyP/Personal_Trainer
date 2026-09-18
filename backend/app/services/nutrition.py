"""减脂营养目标计算：纯代码算好，LLM 只读不准改（和训练指标同一纪律）。

公式链：Mifflin-St Jeor 基础代谢 → 活动系数（按训练量估）→ 减脂缺口
      → 三大营养素分配（蛋白按体重保肌、脂肪不低于下限、碳水吃剩余）。

纪律：
  - 数据不足（缺体重/身高/年龄）→ 返回 available=False，不硬编数字；
  - 缺口不激进：默认 -400 kcal，且不低于 BMR×1.1（低于基础代谢会掉肌肉、代谢适应）；
  - 蛋白质不减：减脂期 1.8g/kg 是保肌下限，热量缺口从碳水和脂肪里扣。
"""
from __future__ import annotations

# 活动系数：按每周训练次数估（久坐 1.2 起，逐级上调）
_ACTIVITY_BY_DAYS = {0: 1.20, 1: 1.30, 2: 1.38, 3: 1.46, 4: 1.55, 5: 1.64, 6: 1.72}

DEFAULT_DEFICIT = 400.0        # 减脂缺口 kcal/天
MIN_BMR_FACTOR = 1.10          # 目标热量不得低于 BMR 的这个倍数
PROTEIN_PER_KG = 1.8           # 减脂期保肌蛋白
FAT_PER_KG = 0.9               # 脂肪下限（激素与脂溶性维生素）
MIN_FAT_KCAL_RATIO = 0.20      # 脂肪至少占总热量 20%


def _bmr_mifflin(weight_kg: float, height_cm: float, age: int, gender: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if (gender or "male").lower().startswith("m") else base - 161


def compute_targets(profile: dict, metrics: dict) -> dict:
    """算每日热量与三大营养素目标。数据不足时 available=False。

    返回：
      available / tdee / target_kcal / protein_g / fat_g / carb_g / note
    """
    weight = profile.get("weight_kg")
    height = profile.get("height_cm")
    age = profile.get("age")
    gender = profile.get("gender") or "male"
    goal = (profile.get("goal") or "health").lower()
    days = profile.get("weekly_available_days") or 0

    if not (weight and height and age):
        return {"available": False,
                "note": "缺体重/身高/年龄，无法算营养目标（去「个人档案」补全）"}

    bmr = _bmr_mifflin(float(weight), float(height), int(age), gender)
    factor = _ACTIVITY_BY_DAYS.get(min(int(days), 6), 1.46)
    tdee = bmr * factor

    if goal in ("fat_loss", "weight_loss"):
        target = max(tdee - DEFAULT_DEFICIT, bmr * MIN_BMR_FACTOR)
        deficit = round(tdee - target)
    else:
        target = tdee
        deficit = 0

    protein_g = round(PROTEIN_PER_KG * float(weight))
    fat_g = max(round(FAT_PER_KG * float(weight)),
                round(target * MIN_FAT_KCAL_RATIO / 9))
    carb_kcal = target - protein_g * 4 - fat_g * 9
    carb_g = max(round(carb_kcal / 4), 0)

    return {
        "available": True,
        "bmr": round(bmr),
        "tdee": round(tdee),
        "target_kcal": round(target),
        "deficit_kcal": deficit,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carb_g": carb_g,
        "note": f"按体重 {weight}kg、每周训练 {days} 天估算",
    }


def render_targets(t: dict) -> str:
    """渲染成可注入 prompt 的一行文本。"""
    if not t.get("available"):
        return f"（营养目标不可用：{t.get('note', '')}）"
    line = (f"每日目标热量 {t['target_kcal']} kcal"
            f"（TDEE {t['tdee']}"
            + (f"，缺口 {t['deficit_kcal']} kcal" if t.get("deficit_kcal") else "")
            + f"）；蛋白质 {t['protein_g']}g / 脂肪 {t['fat_g']}g / 碳水 {t['carb_g']}g"
            + f"。{t.get('note', '')}")
    return line
