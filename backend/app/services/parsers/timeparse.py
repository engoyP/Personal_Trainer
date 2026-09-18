from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.config import settings

# 华为导出的 GPX/TCX 时间戳一律带 Z，例如 2026-09-05T11:45:33.000Z。
# 按 GPX 规范 Z 就是 UTC，这一点实测确认过：同一天那条跑步，华为 App 显示的开始
# 时间是 19:45（北京时间），而文件里写的是 11:45Z —— 正好差 8 小时。
#
# 天真的做法是 `.replace(tzinfo=None)` 直接丢掉 tzinfo，那会把 UTC 的 11:45
# 当成本地 11:45，整整早 8 小时。更要命的是**跨凌晨的运动会被归到前一天** ——
# ATL/CTL 是按 `start_time.date()` 分桶的，日负荷会跟着挪。
#
# 用固定偏移而不用 zoneinfo：中国自 1991 年起没有夏令时，+8 是精确的；
# 而 Windows 上的 zoneinfo 需要额外的 tzdata 包（本机就没有），
# 依赖宿主系统时区又不可移植（换个时区的机器结果就变了）。

# Unix 时间戳的下限（2001-09-09）。比它小的数字只能是相对秒数。
_EPOCH_MIN = 1_000_000_000

_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
    "%Y-%m-%d", "%Y/%m/%d",
)


def local_zone() -> timezone:
    return timezone(timedelta(hours=settings.LOCAL_UTC_OFFSET_HOURS))


def to_local_naive(dt: datetime | None) -> datetime | None:
    """带时区的转成本地钟面时间再去掉 tzinfo；没有时区标记的原样返回。

    「没有时区标记」= 文件里本来就没写 Z 或偏移，那种情况只能当本地时间，
    不能拿系统时区去猜。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(local_zone()).replace(tzinfo=None)


def parse_time(value) -> datetime | None:
    """认 ISO 8601（含 Z / ±HH:MM 偏移）和几种常见写法，统一返回本地朴素时间。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return to_local_naive(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return to_local_naive(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_time_or_epoch(value) -> datetime | None:
    """先按日期写法认，再认 Unix 秒 / 毫秒时间戳。

    ≥1e12 当毫秒，≥1e9 当秒。比这更小的纯数字只可能是相对秒数（"330" 这种），
    返回 None 交给调用方按 elapsed 处理 —— 把 "330" 当成 1970-01-01T00:05:30
    是个很隐蔽的错误。

    数字类型（int/float）和历史字符串两种入参都要认：华为 JSON 里的
    时间戳是数字，CSV 里的是字符串，而它们最终必须是同一个口径。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        if n >= 10 ** 12:
            n /= 1000.0
        if n >= _EPOCH_MIN:
            try:
                return to_local_naive(datetime.fromtimestamp(n, tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                return None
        return None

    text = str(value or "").strip()
    if re.fullmatch(r"\d{10,13}", text) or re.fullmatch(r"\d{10,13}\.\d+", text):
        n = float(text)
        if n >= 10 ** 12:
            n /= 1000.0
        if n >= _EPOCH_MIN:
            try:
                return to_local_naive(datetime.fromtimestamp(n, tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                return None
    return parse_time(text)
