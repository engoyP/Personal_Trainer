from __future__ import annotations

import csv
import io
import re

from .sport import DEFAULT_SPORT, guess_sport, guess_sport_strict
from .timeparse import parse_time, parse_time_or_epoch

# 列名别名。匹配规则见 _alias_score —— 单字符的 latin 别名只认全等，
# 否则 "t" 会命中 "HeartRate"（里面含字母 t），把时间轴指到心率列上。
ALIASES = {
    "t": ["t", "time", "elapsed", "sec", "秒", "时间", "用时"],
    "date": ["date", "日期"],
    "hr": ["hr", "heart", "heartrate", "heart_rate", "bpm", "心率"],
    "cad": ["cad", "cadence", "steprate", "step_rate", "spm", "步频"],
    "speed": ["speed", "velocity", "mps", "速度"],
    "pace": ["pace", "配速"],
    "alt": ["alt", "altitude", "elev", "海拔", "高度"],
    "lat": ["lat", "latitude", "纬度"],
    "lon": ["lon", "lng", "longitude", "经度"],
    "dist": ["dist", "distance", "距离"],
}

# 这几个用全等匹配：子串匹配会把「运动时间」当成「运动」，
# 从而把一个时长列当成运动类型列。
EXACT_ALIASES = {
    "sport": ["sport", "sporttype", "sport_type", "type", "activitytype",
              "运动类型", "活动类型", "锻炼类型", "运动"],
}

# 华为「我的 → 运动数据 → 导出」给的是每天一行的汇总表，
# 没有逐点数据，硬当采样序列算会得到一条 0 km、几秒的假记录。
DAILY_TABLE_REASON = "这份 CSV 像是「每天一行」的汇总表，不是单次运动的逐点明细。"
DAILY_TABLE_ACTION = (
    "请在华为运动健康 App 里打开某一次运动 → 分享/更多 → 导出数据，"
    "得到含时间与心率的逐点 CSV 再导入。"
)
DAILY_TABLE_HINT = DAILY_TABLE_REASON + DAILY_TABLE_ACTION


def _empty(reason: str | None = None) -> dict:
    out = {"sport_type": "unknown", "start_time": None,
           "external_id": None, "samples": []}
    if reason:
        out["error"] = reason
    return out


def _norm(h) -> str:
    """表头归一化：小写、去掉括号里的单位（"速度(m/s)" -> "速度"）。"""
    s = str(h or "").strip().lower()
    s = re.sub(r"[（(\[][^）)\]]*[）)\]]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _alias_score(header: str, alias: str) -> int:
    """3 = 确定命中，2 = 大概率命中，0 = 不匹配。"""
    if not header or not alias:
        return 0
    if header == alias:
        return 3
    if _is_cjk(alias):
        # 中文没有词边界，"平均心率" 要能被 "心率" 命中
        return 2 if alias in header else 0
    if len(alias) < 2:
        # 单字符别名（"t"）只认全等。放开子串匹配的话
        # "HeartRate" 会命中 "t"，时间轴直接跑到心率列上。
        return 0
    if re.search(rf"(?:^|[^a-z0-9]){re.escape(alias)}(?:$|[^a-z0-9])", header):
        return 3
    compact_alias = re.sub(r"[^a-z0-9]", "", alias)
    compact_header = re.sub(r"[^a-z0-9]", "", header)
    if compact_alias and compact_alias == compact_header:
        return 3
    if len(compact_alias) >= 3 and compact_alias in compact_header:
        return 2
    return 0


def _match(headers: list) -> dict:
    norm = [_norm(h) for h in headers]
    cands = []          # (score, std, index)
    for std, keys in ALIASES.items():
        for i, hn in enumerate(norm):
            best = max((_alias_score(hn, k) for k in keys), default=0)
            if best:
                cands.append((best, std, i))
    for std, keys in EXACT_ALIASES.items():
        for i, hn in enumerate(norm):
            if hn in keys:
                cands.append((4, std, i))

    # 分数高的先占列，同一列不会被两个字段抢
    cands.sort(key=lambda c: (-c[0], c[2]))
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for _score, std, i in cands:
        if std in mapping or i in used:
            continue
        mapping[std] = i
        used.add(i)
    return mapping


def _parse_elapsed(s) -> float | None:
    """'5:30' / '1:02:03' / '330' -> 秒。不是这几种就返回 None。"""
    s = str(s).strip()
    if not s:
        return None
    if re.fullmatch(r"[\d.]+", s):
        try:
            return float(s)
        except ValueError:
            return None
    if (re.fullmatch(r"\d{1,3}:[0-5]?\d(\.\d+)?", s)
            or re.fullmatch(r"\d+:\d{1,2}:\d{1,2}(\.\d+)?", s)):
        try:
            nums = [float(p) for p in s.split(":")]
        except ValueError:
            return None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total
    return None


def _parse_time_column(vals: list):
    """把时间列解析成 (相对秒序列, 起始时间, 类型)。

    类型：
      absolute  —— 带时刻的绝对时间（"2026-09-05 11:45:33" / Unix 时间戳）
      date_only —— 只有日期，说明是每天一行的汇总表，不是采样序列
      relative  —— 相对秒数（列名往往是"用时"/"elapsed"），拿不到日期
      none      —— 完全没有可用的时间信息
    """
    n = len(vals)
    dts = [parse_time_or_epoch(v) for v in vals]
    known = [d for d in dts if d is not None]

    if known:
        first_raw = next((str(v).strip() for v in vals if str(v or "").strip()), "")
        has_clock = bool(re.search(r"\d{1,2}:\d{2}", first_raw)) or \
            bool(re.fullmatch(r"\d{10,13}", first_raw))
        start = known[0]
        if not has_clock:
            # "2026-09-05" 这种只有日期的列不是时间轴，用它当 t 会让时长恒为 0
            return [float(i) for i in range(n)], start, "date_only"
        ts = []
        for d in dts:
            if d is None:
                # 中间缺一个值时沿用上一个，避免造出非递增的轴
                ts.append(ts[-1] if ts else 0.0)
            else:
                ts.append((d - start).total_seconds())
        return ts, start, "absolute"

    ts, any_ok = [], False
    for i, v in enumerate(vals):
        e = _parse_elapsed(v) if str(v or "").strip() else None
        ts.append(e if e is not None else float(i))
        any_ok = any_ok or e is not None
    return ts, None, ("relative" if any_ok else "none")


def _distinct_dates(vals: list) -> int:
    seen = set()
    for v in vals:
        d = parse_time(v)
        if d:
            seen.add(d.date())
    return len(seen)


def _find_header(rows: list, limit: int = 10):
    """表头不一定在第一行。

    华为等平台导出的 CSV 前面常有几行说明（标题、导出时间、设备名），
    按「第一行就是表头」硬取的话，`_match` 什么都匹配不到，
    于是解析出一堆全 None 的采样点 —— 不报错，直接落库成一条 0 km 的空壳记录。
    这里在前若干行里挑匹配字段最多的那行当表头。
    """
    best_i, best_map = 0, {}
    for i, row in enumerate(rows[:limit]):
        m = _match(row)
        if len(m) > len(best_map):
            best_i, best_map = i, m
    return best_i, best_map


def _sniff_sport(mapping: dict, preamble: list, data: list) -> str:
    """运动类型：先看专门的列，再从表头之前的说明行里找关键词。"""
    if "sport" in mapping:
        i = mapping["sport"]
        for row in data:
            if i < len(row):
                s = guess_sport_strict(row[i])
                if s:
                    return s
    for row in preamble:
        for cell in row:
            s = guess_sport_strict(cell)
            if s:
                return s
    return guess_sport(None)


def _parse_pace(v) -> float | None:
    """'5:30' 或 "5'30\\"" -> 330 秒每公里"""
    v = str(v).strip()
    try:
        if ":" in v:
            m, s = v.split(":")[:2]
            return float(m) * 60 + float(s)
        if "'" in v:
            m, s = v.replace('"', "").split("'")[:2]
            return float(m) * 60 + float(s)
        return float(v)
    except ValueError:
        return None


def parse_csv(content: bytes) -> dict:
    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 2:
        return _empty("CSV 里没有数据行。")

    hdr_i, mapping = _find_header(rows)
    data = rows[hdr_i + 1:]
    if not data:
        return _empty("CSV 只有表头，没有数据行。")
    if not mapping:
        return _empty("认不出这份 CSV 的列名，至少要能识别出时间或心率列。")

    if "t" not in mapping:
        if "date" in mapping:
            return _empty(DAILY_TABLE_HINT)
        return _empty("CSV 里没有时间列，无法还原时长与配速。")

    t_idx = mapping["t"]
    raw_t = [row[t_idx] if t_idx < len(row) else "" for row in data]
    ts, start, kind = _parse_time_column(raw_t)

    if kind == "date_only":
        return _empty(DAILY_TABLE_HINT)
    if kind == "none":
        return _empty("CSV 的时间列认不出格式（支持 2026-09-05 11:45:33、"
                      "1:02:03、330 秒这几种）。")

    # 日期跨度先查：覆盖多天的表一定是汇总表，这个提示比
    # 「时间不是递增的」更能告诉用户下一步该怎么办
    if "date" in mapping:
        d_idx = mapping["date"]
        n_days = _distinct_dates([row[d_idx] if d_idx < len(row) else "" for row in data])
        if n_days >= 3:
            return _empty(f"这份 CSV 覆盖了 {n_days} 个不同日期，是多天汇总表，"
                          "不是单次运动的逐点明细。" + DAILY_TABLE_ACTION)

    # 单次运动的时间轴必须单调不减。汇总表（每天一行、时长忽长忽短）
    # 或列被认错时，这里会立刻违反。
    if any(ts[i] < ts[i - 1] for i in range(1, len(ts))):
        return _empty("CSV 的时间列不是递增的，不像单次运动的时间轴。")

    warnings: list[str] = []
    sport = _sniff_sport(mapping, rows[:hdr_i], data)
    if kind == "relative":
        warnings.append(
            "文件里只有相对用时、没有日期，这条记录无法进入负荷曲线（ATL/CTL/TSB）"
            "和趋势统计，也无法参与重复检测。"
        )

    def get(key, row):
        i = mapping.get(key)
        if i is None or i >= len(row):
            return None
        v = row[i].strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return v

    samples = []
    for idx, row in enumerate(data):
        hr = get("hr", row)
        cad = get("cad", row)
        speed = get("speed", row)
        if speed is None:
            pace_raw = get("pace", row)
            if isinstance(pace_raw, str):
                p = _parse_pace(pace_raw)
                if p:
                    speed = 1000.0 / p
            elif isinstance(pace_raw, (int, float)) and pace_raw > 0:
                # 纯数字的配速字段，按 秒/公里 处理
                speed = 1000.0 / float(pace_raw)

        samples.append(
            {
                "t": ts[idx],
                "lat": get("lat", row),
                "lon": get("lon", row),
                "alt": get("alt", row),
                "hr": int(hr) if isinstance(hr, (int, float)) else None,
                "cad": float(cad) if isinstance(cad, (int, float)) else None,
                "speed": float(speed) if isinstance(speed, (int, float)) else None,
            }
        )

    # 里程只能从速度/配速/经纬度里来。三者都没有时距离必然是 0，
    # 而落库结果是一个「0 km 但有负荷」的记录 —— 不说一声就等于数据丢了。
    has_speed = any(s["speed"] is not None for s in samples)
    has_geo = any(s["lat"] is not None and s["lon"] is not None for s in samples)
    if not has_speed and not has_geo:
        if "dist" in mapping:
            warnings.append(
                "文件里有距离列，但没有速度/配速/经纬度。距离列目前不参与计算，"
                "这条记录的里程和配速会是 0 和空。"
            )
        else:
            warnings.append("文件里没有速度/配速/经纬度，无法计算里程和配速。")

    out = {
        "sport_type": sport if sport else DEFAULT_SPORT,
        "start_time": start,
        "external_id": None,
        "samples": samples,
    }
    if warnings:
        out["warnings"] = warnings
    return out
