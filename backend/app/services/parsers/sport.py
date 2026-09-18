from __future__ import annotations

# 中英文运动类型关键词。顺序即优先级，命中靠前的先返回。
#
# 为什么必须带中文：华为运动健康导出的 GPX 里 <type> 是中文（"户外跑步"/
# "户外骑行"/"户外步行"），TCX 的 Sport 属性同样可能写中文。只匹配英文时
# 这三类会全部落到默认的 running —— "户外跑步"侥幸正确，但"户外骑行"会
# 被当成跑步，速度阈值（0.8 vs 0.5 m/s）和卡路里 MET 公式都会算错。
_SPORT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cycling", ("cycl", "bik", "骑行", "单车", "自行车", "骑车")),
    ("swimming", ("swim", "游泳", "泳池", "开放水域")),
    ("walking", ("walk", "hik", "步行", "徒步", "健走", "散步", "登山", "越野走")),
    ("running", ("run", "跑步", "越野跑", "慢跑")),
]

DEFAULT_SPORT = "running"


def guess_sport_strict(raw: str | None) -> str | None:
    """只在真的命中关键词时返回类型，否则 None。

    CSV 这类没有 `type` 字段的文件要靠「嗅探」说明行来认运动类型，
    必须能区分「认出来了是跑步」和「什么都没认出来」——用 guess_sport
    的话两者都返回 running，会把一个纯数字说明行误当成跑步。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    for sport, keys in _SPORT_RULES:
        if any(k in text for k in keys):
            return sport
    return None


def guess_sport(raw: str | None) -> str:
    """从运动类型字符串猜标准 sport_type，中英文都认。

    匹配不到时返回 running —— 绝大多数导入都是跑步，猜错方向的代价
    也比抛异常小。想精确控制请在导入后手动改。
    """
    return guess_sport_strict(raw) or DEFAULT_SPORT
