"""从华为运动分享海报 / 截图的 OCR 文本块里解析运动 summary 字段。

设计定位（和「图片不给逐点时序」的顾虑不冲突）：
  - 只抽**汇总字段**（距离、时长、平均/最大心率、步频、步幅、爬升、卡路里、日期时间、类型）；
  - 不伪造采样点 —— 解析结果由调用方展示给用户**核对确认**后才走 create_manual_activity；
  - 负荷按配速估算（和手动录入完全一致），绝不编逐点心率。

不依赖具体 OCR 引擎：输入统一为文本块列表
    [{"text": str, "conf": float, ...}, ...]
输出结构化字段，字段名对齐 routers/activities.py 的 ManualActivityIn。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from .parsers.sport import guess_sport

# 各字段的候选标签（含近似写法，容忍 OCR 误识）。务必用「较长且特异」的词，
# 避免「心」这种单字匹配到无关文本。
_KW = {
    "distance": ["公里", "千米", "km", "距离"],
    "duration": ["时长", "用时", "历时", "运动时间"],
    "max_hr": ["最大心率", "最高心率"],
    "avg_hr": ["平均心率", "心率", "avehr"],
    "cadence": ["步频", "步幅频率", "cadence", "spm"],
    "stride": ["步幅", "步长", "stride"],
    "elev": ["爬升", "累计爬升", "攀升"],
    "calories": ["千卡", "卡路里", "kcal", "热量", "消耗"],
    "sport": ["跑步", "骑行", "骑车", "单车", "步行", "走路", "游泳",
              "徒步", "健走", "户外", "训练"],
}

_NUM = r"(-?\d+(?:\.\d+)?)"
# 时钟 MM:SS / HH:MM，容忍全角冒号（OCR 偶尔吐全角）
_CLOCK = r"(\d{1,2})[:：](\d{2})"
# 三段时长 HH:MM:SS（华为「运动时间」几乎总是三段）
_CLOCK_FULL = r"(\d{1,2})[:：](\d{1,2})[:：](\d{2})"
# 时钟被 OCR 误成小数点分隔（00.34.51 / 00:34.51 都可能）
_CLOCK_DOT = r"(\d{1,2})[.．](\d{2})[.．](\d{2})"


def _num(text: str) -> Optional[float]:
    m = re.search(_NUM, text)
    return float(m.group(1)) if m else None


def _num_tailed_all(text: str, unit_chars: str):
    """产出「数字+单位后缀」的数字及其后缀拆分（99171步 → 99171, 9171, 171, 17, 1）。
    OCR 常把相邻数字并进一个块（如噪声+真值合并），真值是靠近单位的那段。"""
    m = re.search(_NUM + r"\s*(?=[" + unit_chars + r"])", text)
    if not m:
        return
    s = m.group(1).lstrip("-")
    for k in range(len(s)):
        yield float(s[k:])


def _contains_any(text: str, words) -> bool:
    return any(w in text for w in words)


def _scan(blocks, words):
    return [i for i, b in enumerate(blocks) if _contains_any(b["text"], words)]


# 纯单位块：只有单位词、数字无实际意义，取值时必须跳过。
_UNIT_ONLY = re.compile(
    r"^(次|步|米|公里|千米|厘米|千卡|卡路里|kcal|分钟|小时|km|cm|m|bpm|spm|"
    r"步/分|次/分|步/分钟|次/分钟)[/分]?[\d\s]*$",
    re.IGNORECASE,
)


def _is_unit_block(text: str) -> bool:
    """判断文本块是否只是单位（如「次/分」「步/分」「厘米」），不含真正的数值。"""
    t = text.strip()
    if not t:
        return True
    # 含数字但数字被单位词夹住、无独立数值（如「次1分」「步1分」是 OCR 把 / 认成 1）
    if re.search(r"[次步米公里千米厘米千卡][\d/]", t) and not re.search(
        r"\d{2,}", t
    ):
        # 像「97厘米」这种值+单位要保留（数字>=2位且在前）
        if re.match(r"^\d{1,3}(\.\d+)?\s*(厘米|米|公里|千米|cm|m|km|kcal|千卡)", t):
            return False
        return True
    if _UNIT_ONLY.match(t):
        return True
    return False


def _bbox_geom(b) -> Optional[tuple]:
    """从块里取 EasyOCR bbox 的几何量 (x_center, y_center, w, h)。无 bbox 返回 None。"""
    bb = b.get("bbox")
    if not bb or len(bb) != 4:
        return None
    try:
        xs = [float(p[0]) for p in bb]
        ys = [float(p[1]) for p in bb]
    except (TypeError, ValueError):
        return None
    x_center = (min(xs) + max(xs)) / 2.0
    y_center = (min(ys) + max(ys)) / 2.0
    return x_center, y_center, max(xs) - min(xs), max(ys) - min(ys)


def _candidates(blocks, idx):
    """按「最可能是对应值」的顺序产出候选块 (j, text, conf)。

    有 bbox 时按空间关系（华为海报卡片实测规律：值在标签**正下方且 x 对齐**）：
      a) 在标签下方不远处且 x 大体对齐（两列卡片「标签上/值下」）—— 按 y 距离从近到远
      b) 同一行且在标签右侧（行式布局「标签 值」）—— 按 x 距离从近到远
      c) 块序右侧（兜底，与无 bbox 时一致）
    注意 a) 优先于 b)：EasyOCR 检测框常跨行（h 偏大），same_row 误判率高于 x 对齐。
    无 bbox 时只有 c)（合成测试数据 / 旧版行为）。
    """
    lb = blocks[idx]
    geom = _bbox_geom(lb)
    if geom is None:
        # 无 bbox（合成数据/测试）：左右邻近都看，左先于右（值块常在标签前）
        for j in (idx - 1, idx + 1, idx - 2, idx + 2):
            if 0 <= j < len(blocks):
                yield j, blocks[j]["text"], blocks[j]["conf"]
        return
    lx, ly, lw, lh = geom
    same_row, below = [], []
    for j, b in enumerate(blocks):
        if j == idx:
            continue
        g = _bbox_geom(b)
        if g is None:
            continue
        x, y, w, h = g
        if ly < y <= ly + 6 * max(lh, h):                       # a) 下方不远处
            if abs(x - lx) < 0.8 * max(lw, w) + 40:             # x 大体对齐
                below.append((y - ly, j))
        elif abs(y - ly) <= 0.6 * max(lh, h) and x > lx:        # b) 同行右侧
            same_row.append((abs(x - lx), j))
    for _, j in sorted(below):
        yield j, blocks[j]["text"], blocks[j]["conf"]
    for _, j in sorted(same_row):
        yield j, blocks[j]["text"], blocks[j]["conf"]
    for d in range(1, 4):                                       # c) 兜底
        j = idx + d
        if 0 <= j < len(blocks):
            yield j, blocks[j]["text"], blocks[j]["conf"]


def recognize_activity(blocks, year: Optional[int] = None) -> dict:
    if year is None:
        year = date.today().year

    fields: dict = {}
    confidence: dict = {}
    raw: dict = {}
    notes: list = []

    # ---- 运动类型 ----
    for i in _scan(blocks, _KW["sport"]):
        g = guess_sport(blocks[i]["text"])
        if g:
            fields["sport_type"] = g
            confidence["sport_type"] = blocks[i]["conf"]
            raw["sport_type"] = blocks[i]["text"]
            break

    # ---- 距离（公里）----「5.68公里」值常在标签左侧：块内 → 左 → 候选（右/下）
    for i in _scan(blocks, _KW["distance"]):
        found = None
        if not _is_unit_block(blocks[i]["text"]):
            nv = _num(blocks[i]["text"])
            if nv is not None:
                found = (nv, blocks[i]["conf"], blocks[i]["text"])
        if found is None:
            for d in range(1, 3):
                j = i - d
                if 0 <= j < len(blocks):
                    txt = blocks[j]["text"]
                    if _is_unit_block(txt):
                        continue
                    nv = _num(txt)
                    if nv is not None:
                        found = (nv, blocks[j]["conf"], txt)
                        break
        if found is None:
            for j, txt, cf in _candidates(blocks, i):
                if _is_unit_block(txt):
                    continue
                nv = _num(txt)
                if nv is not None:
                    found = (nv, cf, txt)
                    break
        if found is None:
            continue
        v, conf, rt = found
        if not (0 < v < 500):
            continue
        # 单位跨块：值块及其后两块拼上下文
        unit_ctx = rt
        for j in (i + 1, i + 2):
            if 0 <= j < len(blocks):
                unit_ctx += " " + blocks[j]["text"]
        low = unit_ctx.lower()
        if "米" in unit_ctx and "公里" not in unit_ctx and "千米" not in unit_ctx:
            v = v / 1000.0          # 明确以米为单位
        elif "公里" in unit_ctx or "千米" in unit_ctx or re.search(r"\bkm\b", low):
            pass                     # 已是公里
        elif v > 100:                # 无单位且数值过大，大概率是米
            v = v / 1000.0
        fields["distance_km"] = round(v, 3)
        confidence["distance_km"] = conf
        raw["distance_km"] = rt
        break

    # ---- 时长 ----
    # 华为「运动时间」几乎总是 HH:MM:SS 三段；OCR 可能把冒号误成点号（00.34.51）。
    # 三段优先，其次点号三段，最后两段 MM:SS。
    def _duration_from(text):
        m = re.search(_CLOCK_FULL, text)
        if m:
            return (int(m.group(1)) * 60 + int(m.group(2))
                    + int(m.group(3)) / 60.0)
        m = re.search(_CLOCK_DOT, text)
        if m:
            return (int(m.group(1)) * 60 + int(m.group(2))
                    + int(m.group(3)) / 60.0)
        m = re.search(_CLOCK, text)
        if m:
            return int(m.group(1)) + int(m.group(2)) / 60.0
        return None

    for i in _scan(blocks, _KW["duration"]):
        seq = [(blocks[i]["text"], blocks[i]["conf"])]
        seq += [(txt, cf) for _, txt, cf in _candidates(blocks, i)]
        found = None
        for txt, cf in seq:
            vt = _duration_from(txt)
            if vt is not None and 0 < vt < 24 * 60:
                found = (vt, cf, txt)
                break
        if found is None:
            for _, txt, cf in _candidates(blocks, i):
                if _is_unit_block(txt):
                    continue
                nv = _num(txt)
                if nv is not None and 0 < nv < 24 * 60:
                    found = (nv, cf, txt)
                    break
        if found is not None:
            v, conf, rt = found
            fields["duration_min"] = round(v, 2)
            confidence["duration_min"] = conf
            raw["duration_min"] = rt
            break

    # ---- 通用取值闭包：块内优先 → 候选序（bbox 下方对齐/同行右/块序兜底），
    #      第一个落在合理范围的值即命中；带 unit_chars 时优先「数字+单位后缀」
    #      并允许后缀回溯（99171步 → 171步），专治 OCR 数字粘连。----
    def _pick_value(i, lo, hi, unit_chars=None):
        def _from_text(txt):
            """从块文本产出 (v, kind)；kind='tailed' 表示带单位后缀的优先值。"""
            if _is_unit_block(txt):
                return
            if unit_chars:
                for v in _num_tailed_all(txt, unit_chars):
                    yield v
            yield _num(txt)
        v0 = _from_text(blocks[i]["text"])
        for v in v0:
            if v is not None and lo <= v <= hi:
                return v, blocks[i]["conf"], blocks[i]["text"]
        for _, txt, cf in _candidates(blocks, i):
            for v in _from_text(txt):
                if v is not None and lo <= v <= hi:
                    return v, cf, txt
        return None, None, None

    # ---- 最大心率（先于平均心率，避免「心率」命中最大心率块）----
    matched_idx = set()
    for i in _scan(blocks, _KW["max_hr"]):
        v, conf, rt = _pick_value(i, 30, 250)
        if v is not None:
            fields["max_hr"] = v
            confidence["max_hr"] = conf
            raw["max_hr"] = rt
            matched_idx.add(i)
            break

    # ---- 平均心率 ----
    for i in _scan(blocks, _KW["avg_hr"]):
        if i in matched_idx:
            continue
        v, conf, rt = _pick_value(i, 30, 250)
        if v is not None:
            fields["avg_hr"] = v
            confidence["avg_hr"] = conf
            raw["avg_hr"] = rt
            break

    # ---- 步频（值常带「步/次」后缀，如 171步/分钟）----
    for i in _scan(blocks, _KW["cadence"]):
        v, conf, rt = _pick_value(i, 40, 260, unit_chars="步次")
        if v is not None:
            fields["avg_cadence_spm"] = v
            confidence["avg_cadence_spm"] = conf
            raw["avg_cadence_spm"] = rt
            break

    # ---- 步幅（米，支持厘米；单位可能跨块）----
    for i in _scan(blocks, _KW["stride"]):
        cands = [(blocks[i]["text"], blocks[i]["conf"], i)]
        cands += [(txt, cf, j) for j, txt, cf in _candidates(blocks, i)]
        got = None
        for txt, cf, j in cands:
            if _is_unit_block(txt):
                continue
            nv = None
            for cand in _num_tailed_all(txt, "厘米cm"):
                nv = cand
                break
            if nv is None:
                nv = _num(txt)
            if nv is None:
                continue
            unit_ctx = txt
            for k in (j + 1, j + 2):
                if 0 <= k < len(blocks):
                    unit_ctx += " " + blocks[k]["text"]
            if ("厘" in unit_ctx or "厘米" in unit_ctx
                    or "cm" in unit_ctx.lower()):
                nv = nv / 100.0
            elif nv > 3 and nv < 300:   # 厘米漏单位，转米
                nv = nv / 100.0
            if 0.2 <= nv <= 3.0:
                got = (nv, cf, txt)
                break
        if got is not None:
            v, conf, rt = got
            fields["avg_stride_m"] = round(v, 3)
            confidence["avg_stride_m"] = conf
            raw["avg_stride_m"] = rt
            break

    # ---- 爬升 ----
    for i in _scan(blocks, _KW["elev"]):
        v, conf, rt = _pick_value(i, 0, 10000)
        if v is not None:
            fields["elev_gain_m"] = v
            confidence["elev_gain_m"] = conf
            raw["elev_gain_m"] = rt
            break

    # ---- 卡路里（千卡）----
    for i in _scan(blocks, _KW["calories"]):
        v, conf, rt = _pick_value(i, 1, 5000)
        if v is not None:
            fields["calories"] = v
            confidence["calories"] = conf
            raw["calories"] = rt
            break

    # ---- 日期时间：2026/8/29 19:36（斜杠）或 8月29日 19:36 ----
    # 关键约束：
    #   1) 时间只在「含日期」的块里、且**紧跟在日期之后**的位置找（日期和时间的先后是确定的）；
    #   2) 只认 HH:MM（小时 0-23、分钟 0-59），排除三段时长（01:12:37）、配速（6'03"）等干扰；
    #   3) 冒号可能被 OCR 误成点号（19.36）、日期时间可能无空格粘连（2026/8/2919:36）。
    date_m = None
    time_m = None
    date_year = None
    for i, b in enumerate(blocks):
        t = b["text"]
        # 优先斜杠日期（自带年份）
        dm = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", t)
        date_end = None
        if dm:
            yy, mo, dd = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            date_end = dm.end()
            if dd > 31 and len(dm.group(3)) == 2:
                # 日被贪婪匹配吞了时间小时的首位（2026/9/519:45 → 日=51）：
                # 真日是个位，回退一位，把时间首位还给 tail
                dd = int(dm.group(3)[0])
                date_end -= 1
        else:
            dm2 = (re.search(r"(\d{1,2})月(\d{1,2})日?", t)
                   or re.search(r"(\d{1,2})月(\d{1,2})", t))
            if not dm2:
                continue
            yy, mo, dd = year, int(dm2.group(1)), int(dm2.group(2))
            date_end = dm2.end()
        if not (1 <= mo <= 12 and 1 <= dd <= 31):
            continue
        date_m = (mo, dd)
        date_year = yy
        # 时间只在「日期子串之后」的剩余文本里找（同块），再扩展到相邻块
        # 用日期匹配的结束位置截取，避免匹配到日期里的数字
        tail = t[date_end:]
        # 相邻块也拼进来（日期和时间可能分块），但只取日期块右侧紧邻的块
        for j in (i + 1, i - 1):
            if 0 <= j < len(blocks):
                tail += " " + blocks[j]["text"]
        # 找时间：HH:MM 或 HH.MM，小时须 0-23、分钟 0-59
        tm = re.search(r"(\d{1,2})[:：.](\d{2})", tail)
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                time_m = (hh, mm)
    if date_m and time_m:
        try:
            st = datetime(date_year or year, date_m[0], date_m[1],
                          time_m[0], time_m[1])
            fields["start_time"] = st.isoformat()
            confidence["start_time"] = 0.9
            raw["start_time"] = f"{date_year or year}-{date_m[0]:02d}-{date_m[1]:02d} {time_m[0]}:{time_m[1]:02d}"
        except ValueError:
            notes.append("日期/时间数值非法，未生成开始时间")
    elif date_m:
        notes.append("识别到日期但没识别到时间，开始时间留空请手动补")

    # ---- 必填校验提示 ----
    for req in ("sport_type", "start_time", "duration_min", "distance_km"):
        if req not in fields:
            notes.append(f"未识别到必填项：{req}")
    if "avg_hr" not in fields and "max_hr" not in fields:
        notes.append("未识别到心率（将按配速估算负荷）")

    return {"fields": fields, "confidence": confidence, "raw": raw, "notes": notes}
