"""结构化抽取：把一篇文章正文压成可检索、可喂给教练的结构化字段。

抽取失败的降级路径很重要 —— 网络抖动、模型抽风、返回不是 JSON 都可能发生，
不能因为一篇抽出问题就让整轮爬取断掉。降级时用规则兜底（标题、日期、
分类靠关键词），摘要留空，文章照样入库，只是详情页少点东西。

抽取为什么要分段：见下面 MAX_BODY_CHARS 上面的注释。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from app.knowledge.scoring import _prose_only, classify, classify_blob, evidence_level_of
from app.services import llm

# ---------------------------------------------------------------- 正文切分
# 早期做法是「头 4000 + 中间省略 + 尾 1000」。实测站不住：
#   库里 75 篇有 48 篇（64%）原文超 5000 字，最长 53,438 字。
#   《Sleep and Immunity》(52,949 字) 有 90.6% 的正文没进模型 —— 中段的
#   「Sleep and Vaccines」「Sleep and Allergies」「How Can You Improve Sleep」
#   全被丢掉，摘要和要点只剩开头的泛泛过渡句；而摘要+要点是向量召回的文本、
#   重排的文本、也是喂给教练的循证依据，中段内容等于对下游不存在。
# 现在改成：先去掉图片行/链接密集行（实测能砍掉 62~75% 的噪声），再按子标题
# 边界切成最多 MAX_SEGMENTS 段分别抽取，最后合并成一张卡片。
# 为什么总预算取 2.5 万字：实测去噪后的「有效正文」最长 21,654 字、中位 4,003 字，
#   5 段 × 5000 字能完整覆盖全库；而且段数由正文长度决定，只有有效正文超 2 万字
#   的那 1 篇才会真的用到第 5 段，其余文章的多段是长度本身就需要的，不额外花钱。
SEG_CHARS = 5000                            # 单次送模型的正文上限
MAX_SEGMENTS = 5                            # 一篇文章最多切几段
MAX_BODY_CHARS = SEG_CHARS * MAX_SEGMENTS   # 有效正文总预算（25000）
MAX_KEY_POINTS = 8                          # 合并后保留的要点上限

EXTRACT_SYSTEM = """你是运动科学与健康领域的中文资料编辑。你的任务是把一篇网页文章整理成结构化的资料卡片。

⚑ 第一件事：判断这个页面到底是不是「一篇文章」。
爬虫抓回来的页面里混着大量非文章内容：站点索引、栏目导航、Cookie/隐私声明、
登录页、活动报名页，以及因为 JS 没渲染完只剩导航框架的空白页。
如果是这类页面，把 substantive 设为 false 并直接返回，summary 和 key_points 留空，
不要勉强从导航文字里编出摘要 —— 那比空着更有害。

要求：
1. 只依据文章内容，不添加外部知识，不臆测。文章里没提到的一律留空或 null。
2. summary 用中文写 2-3 句，说清「这篇文章讲了什么、结论是什么」，不要写"本文介绍了"这类空话。
3. key_points 提炼 3-5 条可执行或可验证的要点，每条一句话，尽量带上原文里的具体数字（配速、心率区间、剂量、时长、组数次数等）。
4. category 只能是 training / nutrition / health / recovery / injury / weight_loss 之一。
5. evidence_level 按文章性质判断：guideline(指南/专家共识) / review(系统评价/综述) / rct(随机对照试验) / popular(科普文章)。
6. published_at 输出 YYYY-MM-DD；文章里找不到确切日期就 null，不要猜。
7. organization 填发布机构或期刊名（如「美国运动医学会」「British Journal of Sports Medicine」）。

严格输出 JSON：
{
  "substantive": true,
  "title": "标题",
  "organization": "发布机构",
  "author": "作者，没有则空字符串",
  "published_at": "YYYY-MM-DD 或 null",
  "category": "training",
  "topics": ["主题标签"],
  "summary": "中文摘要",
  "key_points": ["要点1", "要点2"],
  "evidence_level": "popular"
}"""

# 长文分段抽取后的合并提示。合并阶段看不到原文，只能做「忠实归并」——
# 所以必须明确禁止它补写原文没有的内容，否则长文的摘要会比短文更容易编。
MERGE_SYSTEM = """你是运动科学与健康领域的中文资料编辑。下面是同一篇文章被分段抽取后得到的若干份局部结果。

要求：
1. 只做归并，不新增原文没有的信息，不臆测，不做任何外部知识补充。
2. summary：合并成一段中文摘要，2-4 句，说清「整篇文章讲了什么、结论是什么」。
   必须覆盖各段的主要话题，不要只保留第一段的内容。
3. key_points：合并去重，去掉那些"很重要""有助于健康"式的泛泛表述，
   优先保留带具体数字/剂量/时长/频率/组数的条目；同类项合并成一条；
   按重要性排序，输出 4-6 条。
4. topics 合并去重，最多 8 个。
5. organization / author 取局部结果里有值的那个；published_at 取非空且最早的那个。
6. category 取多数段落的取值；evidence_level 取证据级别最高的那个
   （guideline > review > rct > popular > unknown）。
7. substantive 一律输出 true。

严格输出 JSON：
{
  "substantive": true,
  "title": "标题",
  "organization": "发布机构",
  "author": "作者",
  "published_at": "YYYY-MM-DD 或 null",
  "category": "training",
  "topics": ["主题标签"],
  "summary": "合并后的中文摘要",
  "key_points": ["要点1", "要点2"],
  "evidence_level": "popular"
}"""

VALID_CATEGORIES = {"training", "nutrition", "health", "recovery", "injury", "weight_loss"}
VALID_EVIDENCE = {"guideline", "review", "rct", "popular", "unknown"}
_EVIDENCE_RANK = {"guideline": 4, "review": 3, "rct": 2, "popular": 1, "unknown": 0}

_HEADING_RE = re.compile(r"^#{1,4}\s")
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
# 整块丢掉的「非正文」小节。要求整行标题**只**由这些词构成 ——
# 用 `Sources` 开头就匹配的话，《Sources of Protein》这种真章节会被误杀。
_NOISE_HEADING_RE = re.compile(
    r"^#{1,4}\s*\[?\s*("
    r"references?|sources?|citations?|bibliography|external links?|further reading|"
    r"related (articles|posts|reading)|still have questions|about (our|the) editorial team|"
    r"参考文献|参考资料|参考来源|相关阅读|延伸阅读|编辑团队"
    r")\s*\]?\s*[:：]?\s*$",
    re.I,
)


# ---------------------------------------------------------------- 切分
def _trim(text: str, limit: int = SEG_CHARS) -> str:
    """单段仍然超限时的兜底：头六尾四。开头有论点、结尾有结论。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n\n...(中间略)...\n\n" + text[-(limit - head):]


def _split_blocks(text: str) -> list[str]:
    """按 markdown 子标题切块，标题归属它下面那段正文。

    顺手丢掉「参考文献 / 相关阅读 / 编辑团队」这类整块非正文 —— 全库实测
    这类块占有效正文的 6.3%，但在 Sleep Foundation 那批长文里 `## References`
    能独占一整段（4500 字），等于白烧掉一次模型调用。
    """
    blocks: list[str] = []
    cur: list[str] = []
    for ln in text.splitlines():
        if _HEADING_RE.match(ln.strip()) and cur:
            blocks.append("\n".join(cur).strip())
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append("\n".join(cur).strip())
    out: list[str] = []
    for b in blocks:
        if not b:
            continue
        head = b.split("\n", 1)[0].strip()
        if _NOISE_HEADING_RE.match(head):
            continue
        out.append(b)
    return out


def _sentence_split(text: str, limit: int) -> list[str]:
    """按句子累加切片，保证每片 <= limit。单句超限就硬切。"""
    out: list[str] = []
    cur = ""
    for raw in _SENT_RE.findall(text):
        s = raw.strip()
        if not s:
            continue
        while len(s) > limit:
            out.append(s[:limit])
            s = s[limit:]
        if len(cur) + len(s) <= limit:
            cur += s
            continue
        if cur:
            out.append(cur)
        cur = s
    if cur:
        out.append(cur)
    return out


def _hard_split(block: str, limit: int = SEG_CHARS) -> list[str]:
    """块内继续切：先按空行分段，单段仍超限再按句子切。

    这一步是必需的 —— 有一部分源（如 ACSM 的页面）抓下来正文里**没有任何
    markdown 子标题**，`_split_blocks` 会退化成「整篇一块」。少了这一层，
    8000 字的正文会被当成一块再被 `_trim` 砍到 5000，覆盖率掉到 60% 以下。
    """
    if len(block) <= limit:
        return [block]
    chunks: list[str] = []
    cur = ""
    for para in (p for p in re.split(r"\n\s*\n", block) if p.strip()):
        pieces = [para] if len(para) <= limit else _sentence_split(para, limit)
        for piece in pieces:
            if not cur:
                cur = piece
            elif len(cur) + len(piece) + 2 <= limit:
                cur = f"{cur}\n\n{piece}"
            else:
                chunks.append(cur)
                cur = piece
    if cur:
        chunks.append(cur)
    return [_trim(c, limit) for c in chunks]


def _segment(text: str) -> list[str]:
    """把去噪后的正文切成不超过 MAX_SEGMENTS 段，每段 <= SEG_CHARS。

    三级降级：子标题边界 → 块内空行/句子边界 → 段数超限时等间隔抽样。
    前两级保住语义完整；第三级只在超长文上触发，抽样的目的是让覆盖率
    均匀摊在整篇上，而不是留头砍尾（那正是这套代码要修的病）。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= SEG_CHARS:
        return [text]

    chunks: list[str] = []
    for b in _split_blocks(text):
        chunks.extend(_hard_split(b))

    segs: list[str] = []
    cur = ""
    for c in chunks:
        cand = f"{cur}\n\n{c}" if cur else c
        if len(cand) <= SEG_CHARS:
            cur = cand
            continue
        if cur:
            segs.append(cur)
        cur = c
    if cur:
        segs.append(cur)

    if len(segs) <= MAX_SEGMENTS or MAX_SEGMENTS <= 1:
        return segs[:MAX_SEGMENTS]

    step = (len(segs) - 1) / (MAX_SEGMENTS - 1)
    return [segs[round(i * step)] for i in range(MAX_SEGMENTS)]


# ---------------------------------------------------------------- LLM 两阶段
def _llm_fields(title: str, body: str, fallback_date, domain: str) -> dict | None:
    """跑一次 LLM 抽取。失败返回 None，由调用方决定降级。"""
    user = f"""标题：{title or '（无）'}
来源域名：{domain or '（未知）'}
网页标注日期：{fallback_date or '（无）'}

正文：
{body}"""
    try:
        data = llm.json_completion(EXTRACT_SYSTEM, user, temperature=0.2, timeout=90)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _render_part(i: int, part: dict) -> str:
    points = part.get("key_points") or []
    plist = "\n".join(f"- {p}" for p in points) if isinstance(points, list) else ""
    topics = part.get("topics") or []
    tlist = "、".join(str(t) for t in topics) if isinstance(topics, list) else ""
    return (f"【第 {i} 段】\n"
            f"摘要：{part.get('summary') or '（无）'}\n"
            f"要点：\n{plist or '（无）'}\n"
            f"主题：{tlist or '（无）'}\n"
            f"分类：{part.get('category') or ''}｜证据级别：{part.get('evidence_level') or ''}")


def _llm_merge(title: str, parts: list[dict], fallback_date, domain: str) -> dict | None:
    """把各段结果合并成一张卡片。失败返回 None。"""
    joined = "\n\n".join(_render_part(i, p) for i, p in enumerate(parts, 1))
    head = f"标题：{title or '（无）'}\n来源域名：{domain or '（未知）'}\n网页标注日期：{fallback_date or '（无）'}\n\n{joined}"
    try:
        data = llm.json_completion(MERGE_SYSTEM, head[:20_000], temperature=0.1, timeout=90)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _merge_fallback(parts: list[dict]) -> dict:
    """合并调用失败时的确定性兜底：挑要点最多的一段当底，其余要点并进来。"""
    base = max(parts, key=lambda p: len(p.get("key_points") or []))
    seen: set[str] = set()
    points: list[str] = []
    for p in [base, *parts]:
        for pt in (p.get("key_points") or []):
            s = str(pt).strip()
            if s and s not in seen:
                seen.add(s)
                points.append(s)
    best = max(parts, key=lambda p: _EVIDENCE_RANK.get(str(p.get("evidence_level") or ""), 0))
    return {
        **base,
        "summary": base.get("summary") or next(
            (p.get("summary") for p in parts if p.get("summary")), ""),
        "key_points": points[:MAX_KEY_POINTS],
        "topics": base.get("topics") or [],
        "evidence_level": best.get("evidence_level") or base.get("evidence_level") or "unknown",
    }


# ---------------------------------------------------------------- 归一化
def parse_date(raw) -> date | None:
    """把各种日期写法收敛成 date。"""
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"(\d{4})[-/年.](\d{1,2})", s)
        if not m:
            return None
        y, mo = int(m.group(1)), int(m.group(2))
        d = 1
    try:
        out = date(y, mo, d)
    except ValueError:
        return None
    # 明显不合理的日期（比今天还晚、或早于 1990）当没有
    today = date.today()
    if out > today or out.year < 1990:
        return None
    return out


def _finalize(data: dict, title: str, prose: str, fb_date, domain: str,
              note: str) -> dict:
    """把模型返回的字段收敛到合法取值。data 已在调用前确认是 dict。"""
    cat = str(data.get("category") or "").strip()
    if cat not in VALID_CATEGORIES:
        cat = classify(classify_blob(prose, title))[0]

    ev = str(data.get("evidence_level") or "").strip()
    if ev not in VALID_EVIDENCE:
        ev = evidence_level_of(domain, prose, title)

    points = data.get("key_points") or []
    if not isinstance(points, list):
        points = []
    points = [str(p).strip() for p in points if str(p).strip()][:MAX_KEY_POINTS]

    topics = data.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(t).strip() for t in topics if str(t).strip()][:8]

    return {
        "substantive": True,
        "title": (str(data.get("title") or "").strip() or title)[:500],
        "organization": str(data.get("organization") or "").strip()[:256],
        "author": str(data.get("author") or "").strip()[:256],
        "published_at": parse_date(data.get("published_at")) or fb_date,
        "category": cat,
        "topics": topics,
        "summary": str(data.get("summary") or "").strip(),
        "key_points": points,
        "evidence_level": ev,
        "extract_note": note,
    }


def _not_article(title: str, fb_date) -> dict:
    """模型判定这不是文章页 → 原样带回去，由 store 决定丢弃。"""
    return {
        "substantive": False,
        "title": title, "organization": "", "author": "",
        "published_at": fb_date, "category": "training", "topics": [],
        "summary": "", "key_points": [], "evidence_level": "unknown",
        "extract_note": "模型判定非文章页",
    }


# ---------------------------------------------------------------- 入口
def extract(title: str, text: str, fallback_date=None, domain: str = "") -> dict:
    """返回结构化字段。LLM 失败时降级到规则抽取，绝不抛异常。"""
    fb_date = parse_date(fallback_date)
    prose = _prose_only(text or "")
    if not prose or len(prose) < 120:
        return _rule_based(title, prose, fb_date, domain, note="正文过短")

    segs = _segment(prose)

    # 单段：沿用原来的单次调用路径
    if len(segs) <= 1:
        data = _llm_fields(title, prose, fallback_date, domain)
        if data is None:
            return _rule_based(title, prose, fb_date, domain, note="LLM 抽取失败")
        if data.get("substantive") is False:
            return _not_article(title, fb_date)
        return _finalize(data, title, prose, fb_date, domain, note="llm")

    # 多段：逐段抽取 → 合并。逐段并发没意义（同一篇文章的内部阶段，
    # 并发只会让 DeepSeek 端先限流后排队），顺序跑更稳、日志也更好读。
    parts: list[dict] = []
    for i, seg in enumerate(segs):
        data = _llm_fields(title, seg, fallback_date, domain)
        if data is None:
            continue
        # 非文章判定只看第一段 —— 导航/索引页的头部最能说明问题，
        # 而长文中某一小段恰好没有信息量是正常的，不能因此整篇丢掉。
        if i == 0 and data.get("substantive") is False:
            return _not_article(title, fb_date)
        parts.append(data)

    n = len(segs)
    if not parts:
        return _rule_based(title, prose, fb_date, domain, note=f"LLM 分段抽取失败（{n} 段）")
    if len(parts) == 1:
        return _finalize(parts[0], title, prose, fb_date, domain, note=f"llm·{n}段取1")

    merged = _llm_merge(title, parts, fallback_date, domain)
    if merged is None:
        merged = _merge_fallback(parts)
        return _finalize(merged, title, prose, fb_date, domain, note=f"llm·{n}段兜底合并")
    if merged.get("substantive") is False:
        merged["substantive"] = True
    return _finalize(merged, title, prose, fb_date, domain, note=f"llm·{n}段合并")


def _rule_based(title: str, body: str, fb_date, domain: str, note: str) -> dict:
    """LLM 不可用时的兜底：分类靠关键词，摘要截开头，要点留空。"""
    cat, topics, _ = classify(classify_blob(body, title))
    head = re.sub(r"\s+", " ", body[:400]).strip()
    return {
        # 降级路径没能力判断页面性质，乐观放行 —— 让链接密度闸门去挡
        "substantive": True,
        "title": (title or "").strip()[:500],
        "organization": "",
        "author": "",
        "published_at": fb_date,
        "category": cat,
        "topics": topics[:8],
        "summary": (head[:280] + "…") if len(head) > 280 else head,
        "key_points": [],
        "evidence_level": evidence_level_of(domain, body, title),
        "extract_note": note,
    }
