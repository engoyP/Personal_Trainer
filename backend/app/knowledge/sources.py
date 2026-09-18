"""权威站点清单 + 域名后缀权重。

选源原则：只要「有署名、有编辑审核、内容可追溯到循证依据」的站点。
自媒体、聚合站、SEO 农场一律不进——权重再低也没必要浪费一次抓取。
"""
from __future__ import annotations

# ------------------------------------------------------------------ 域名后缀权重
# 按「谁在说话」定基础权威分，0-1。
# 顺序敏感：从最具体到最宽泛，命中即返回。
SUFFIX_WEIGHTS: list[tuple[str, float]] = [
    (".gov.cn", 1.00), (".gov", 1.00),          # 政府/卫生主管部门
    (".edu.cn", 0.95), (".edu", 0.95),
    (".ac.cn", 0.95), (".ac.uk", 0.95),         # 中科院 / 英国学术
    (".org.cn", 0.85), (".org", 0.85),          # 学会、协会、NGO
    (".int", 0.90),                             # WHO 之类的国际组织
    (".nhs.uk", 1.00), (".who.int", 1.00),
    (".com.cn", 0.55), (".cn", 0.55),
    (".com", 0.50), (".net", 0.50), (".io", 0.50), (".co", 0.50),
]

DEFAULT_SUFFIX_WEIGHT = 0.35  # 认不出来的域名，不值得信

# ------------------------------------------------------------------ 机构白名单
# 域名后缀只是粗筛，这些是「后缀普通但内容最权威」的，单独给高分。
DOMAIN_WHITELIST: dict[str, float] = {
    # 国际循证 / 临床
    "mayoclinic.org": 1.00,
    "health.harvard.edu": 1.00,
    "acsm.org": 1.00,               # 美国运动医学会
    "nih.gov": 1.00,
    "pubmed.ncbi.nlm.nih.gov": 0.98,
    "ncbi.nlm.nih.gov": 0.98,
    "cdc.gov": 1.00,
    "nhs.uk": 1.00,
    "who.int": 1.00,
    "cochrane.org": 1.00,           # 系统评价金标准
    "bjsm.bmj.com": 1.00,           # British Journal of Sports Medicine
    "sportsmedicine-open.springeropen.com": 0.95,
    "strengthandconditioning.org": 0.95,  # NSCA
    "sleepfoundation.org": 0.85,
    "heart.org": 0.95,              # AHA
    "diabetes.org": 0.90,
    # 国内
    "sport.gov.cn": 1.00,           # 国家体育总局
    "nhc.gov.cn": 1.00,             # 国家卫健委
    "cma.org.cn": 0.95,             # 中华医学会
    "chinacdc.cn": 0.95,
    "cctv.com": 0.60,
    "people.com.cn": 0.60,
    "dxy.cn": 0.80,                 # 丁香园
    "dxy.com": 0.75,
    "haodf.com": 0.70,
    "cnki.net": 0.90,
    "wanfangdata.com.cn": 0.85,
}

# ------------------------------------------------------------------ 主题关键词
# 用来算「这篇文章跟我们关心的事有多相关」。
#
# ⚠️ 这份词表必须含最基础的通用词（exercise / fitness / workout / 跑步 / 锻炼 …）。
# 第一版漏了它们，结果一篇讲运动科学的正规文章相关度只有 0.15 被误杀 ——
# 因为文章通篇说 "exercise" 而我们只在找 "training plan"「间歇」这种窄词。
# 英文词按词边界匹配，所以不用担心 "rest" 命中 "restaurant"。
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "training": [
        # 中文
        "训练", "跑步", "运动", "锻炼", "健身", "跑量", "配速", "间歇", "节奏跑",
        "长距离", "周期化", "训练计划", "力量训练", "核心", "专项", "超量恢复",
        "最大摄氧量", "乳酸阈", "跑姿", "步频", "步幅", "训练强度", "耐力", "体能",
        # 英文
        "exercise", "exercises", "exercising", "fitness", "workout", "workouts",
        "physical activity", "training", "run", "runs", "running", "runner",
        "jog", "jogging", "marathon", "interval", "tempo", "periodization",
        "vo2max", "vo2 max", "lactate threshold", "endurance", "aerobic",
        "anaerobic", "strength", "resistance training", "plyometric", "cardio",
        "conditioning", "warm-up", "cooldown", "mileage", "pace",
    ],
    "recovery": [
        "恢复", "睡眠", "休息", "拉伸", "放松", "按摩", "疲劳", "过度训练",
        "肌肉酸痛", "冷疗", "冰浴", "心率变异", "静息心率",
        "recovery", "sleep", "sleeping", "rest", "overtraining", "stretching",
        "mobility", "foam roll", "doms", "soreness", "hrv", "cold plunge",
    ],
    "injury": [
        "伤病", "损伤", "膝", "跟腱", "足底筋膜", "胫骨", "髂胫束", "拉伤",
        "疼痛", "康复", "预防", "跑步膝", "扭伤",
        "injury", "injuries", "tendon", "achilles", "plantar", "itbs",
        "shin splint", "rehab", "rehabilitation", "prevention", "sprain",
        "strain", "stress fracture", "patellofemoral",
    ],
    "nutrition": [
        "营养", "饮食", "蛋白", "碳水", "补剂", "电解质", "水分", "补给",
        "肌酸", "咖啡因", "减脂", "热量", "膳食",
        "nutrition", "diet", "protein", "carb", "carbohydrate", "hydration",
        "electrolyte", "creatine", "caffeine", "supplement", "calorie",
        "calories", "fueling", "glycogen", "macronutrient",
    ],
    "health": [
        "心率", "血压", "血糖", "体脂", "心脏", "健康", "体检", "代谢",
        "免疫", "骨密度", "慢性病", "心血管",
        "heart rate", "blood pressure", "body fat", "metabolic", "health",
        "cardiovascular", "cholesterol", "diabetes", "hypertension",
        "bone density", "immune", "wellness", "sedentary",
    ],
    "weight_loss": [
        "减重", "减肥", "体重", "瘦身", "热量缺口", "基础代谢", "肥胖",
        "weight loss", "losing weight", "body weight", "bodyweight",
        "caloric deficit", "bmi", "obesity", "energy balance",
    ],
}


# ------------------------------------------------------------------ 主题原型
# 知识库「该收什么」的原型查询，交给本地 Qwen3-Reranker 判定一篇候选文章
# 值不值得入库（比关键词命中准得多：关键词挡不住的《报废固定资产处置公示》
# 也会因为标题带「医学」而算到 0.42 的相关度）。
#
# 用中文写就行 —— Qwen3-Reranker 本身是跨语言的，实测中文原型能正确给
# 英文文章打分（英文相关文档得 -2.8，无关文档 -9 以下）。
TOPIC_PROTOTYPES: list[str] = [
    "跑步与耐力训练的课表安排、训练强度、配速与跑量",
    "力量训练的动作选择、组数次数与渐进超负荷",
    "运动营养：蛋白质、碳水、补剂、补水与能量补给",
    "运动损伤的预防、康复与疼痛处理",
    "睡眠、恢复、过度训练与疲劳管理",
    "身体活动与健康：心血管、体重管理与慢病预防",
]


def suffix_weight(domain: str) -> float:
    """按域名后缀给基础权威分。"""
    d = (domain or "").lower().strip()
    for suffix, w in SUFFIX_WEIGHTS:
        if d.endswith(suffix):
            return w
    return DEFAULT_SUFFIX_WEIGHT


def domain_score(domain: str, source_authority: float | None = None) -> float:
    """域名权威分 = 白名单 / 后缀 / 站点自定义 三者取最大。

    取最大而不是加权平均：白名单里的 .com 站点（比如 mayoclinic.org）
    不该被通用的 .com=0.5 拖下来。
    """
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]

    candidates = [suffix_weight(d)]
    if d in DOMAIN_WHITELIST:
        candidates.append(DOMAIN_WHITELIST[d])
    else:
        # 支持子域匹配，比如 health.harvard.edu 命中 harvard.edu
        for host, w in DOMAIN_WHITELIST.items():
            if d.endswith("." + host):
                candidates.append(w)
                break
    if source_authority is not None:
        candidates.append(float(source_authority))
    return round(max(candidates), 3)


# ------------------------------------------------------------------ 站点种子
# authority 是站点级提权；留空则纯靠域名后缀算分。
SOURCES: list[dict] = [
    # ---------------- 国内 · 官方 / 学术
    #
    # ⚠️ 选中文源的教训：不能指首页。
    # 中文政府/学会站的首页几乎全是**机构新闻**（工作会议、党建活动、
    # 领导出访、资产公示），不是知识文章。英文源之所以能用，是因为它们的
    # 落地页本身就是主题枢纽（harvard.edu/topics/exercise-and-fitness、
    # nhs.uk/live-well/exercise、cdc.gov/physical-activity-basics）。
    # 所以这里一律指向**科普/主题栏目页**，并在下面 DISABLED_SOURCES 里
    # 记下那些「整站都是机构新闻、没有可用栏目」的源。
    {
        "name": "国家体育总局体育科学研究所",
        "domain": "ciss.cn",
        # /kpwz/ 科普文章、/kzjskp/ 科学健身科普 —— 本所是国内运动科学
        # 最对口的官方机构，文章如《体育专家解读22个健身误区》系列。
        "list_urls": [
            "https://www.ciss.cn/kpwz/index.html",
            "https://www.ciss.cn/kzjskp/index.html",
        ],
        "lang": "zh", "category": "training", "authority": 0.95,
        "note": "运动科学科普，权威度最高的中文训练依据来源",
    },
    {
        "name": "中国疾病预防控制中心",
        "domain": "chinacdc.cn",
        # /jkts/ 健康提示（主题栏目）。首页 /gzdt/ 是疾控工作动态，已弃用。
        "list_urls": ["https://www.chinacdc.cn/jkts/"],
        "lang": "zh", "category": "health", "authority": 0.95,
        "note": "慢病防控与健康提示；注意 meta 标题是站点名，靠正文 H1 纠正",
    },
    {
        "name": "丁香医生",
        "domain": "dxy.com",
        # /articles 是科普文章列表（首页是 JS 应用，只有十几条链接）
        "list_urls": ["https://dxy.com/articles"],
        "lang": "zh", "category": "health", "authority": 0.75,
        "note": "科普向，有医学编辑审核；列表是混合流，靠闸门筛",
    },
    # ---------------- 中文 · 营养（2026-09-18 为「饮食计划」补的权威源）
    {
        "name": "中国营养学会·膳食指南",
        "domain": "dg.cnsoc.org",
        # 中国营养学会官方膳食指南站（《中国居民膳食指南（2022）》），
        # 营养领域最权威的中文来源。实测三个栏目各能发现 12 篇、正文 700~2200 字。
        "list_urls": [
            "https://dg.cnsoc.org/newslist_0402_1.htm",       # 膳食指南（2022）平衡膳食
            "https://dg.cnsoc.org/gzdtnewslist_0406_2_1.htm",  # 指南解读
            "https://dg.cnsoc.org/imgnewslist_0602_1.htm",     # 图示和工具
        ],
        "lang": "zh", "category": "nutrition", "authority": 1.0,
        "note": "营养领域最权威中文源；准则条目可直接作为饮食建议依据",
    },
    {
        "name": "上海市疾控中心",
        "domain": "scdc.sh.cn",
        # 只取 /shjk/rdxx/（热点信息）——健康科普。工作动态/通知公告是行政内容，不要。
        "list_urls": ["https://www.scdc.sh.cn/shjk/rdxx/index.html"],
        "lang": "zh", "category": "health", "authority": 0.95,
        "note": "疾控健康科普（热点信息栏目）；别把通知公告/工作动态当文章",
    },
    # ---------------- 国际 · 循证 / 临床
    {
        "name": "ACSM 美国运动医学会",
        "domain": "acsm.org",
        "list_urls": [
            "https://www.acsm.org/education-resources/trending-topics-resources",
            "https://www.acsm.org/blog",
        ],
        "lang": "en", "category": "science", "authority": 1.0,
        "note": "运动处方与训练科学最权威来源之一",
    },
    {
        "name": "Harvard Health",
        "domain": "health.harvard.edu",
        "list_urls": ["https://www.health.harvard.edu/topics/exercise-and-fitness"],
        "lang": "en", "category": "health", "authority": 1.0,
        "note": "哈佛医学院，循证科普",
    },
    {
        "name": "NHS UK",
        "domain": "nhs.uk",
        "list_urls": ["https://www.nhs.uk/live-well/exercise/"],
        "lang": "en", "category": "health", "authority": 1.0,
        "note": "英国国家医疗服务体系，运动指南",
    },
    {
        "name": "WHO 世界卫生组织",
        "domain": "who.int",
        # 只指向「身体活动」这一份实况报道，而不是 /fact-sheets 总索引。
        # 实测总索引虽然可达（270 条链接），但绝大多数是无关主题的实况报道，
        # 会把每源 12 篇的配额填满没用的东西。一份高相关指南胜过 12 条噪声。
        "list_urls": [
            "https://www.who.int/news-room/fact-sheets/detail/physical-activity",
        ],
        "lang": "en", "category": "health", "authority": 1.0,
        "note": "全球身体活动指南，政策级依据",
    },
    {
        "name": "CDC 美国疾控中心",
        "domain": "cdc.gov",
        "list_urls": ["https://www.cdc.gov/physical-activity-basics/"],
        "lang": "en", "category": "health", "authority": 1.0,
        "note": "身体活动基础指南",
    },
    {
        "name": "Cochrane 系统评价",
        "domain": "cochrane.org",
        "list_urls": ["https://www.cochrane.org/evidence"],
        "lang": "en", "category": "science", "authority": 1.0,
        "note": "系统评价金标准，evidence_level=review",
    },
    {
        "name": "NSCA 美国体能协会",
        "domain": "strengthandconditioning.org",
        "list_urls": ["https://www.strengthandconditioning.org/"],
        "lang": "en", "category": "training", "authority": 0.95,
        "note": "力量与体能训练专业组织",
    },
    {
        "name": "Sleep Foundation",
        "domain": "sleepfoundation.org",
        # 实测：/exercise-and-sleep 只有 60 字符（JS 兜底页），/physical-health 可达（61 条）
        # /sleep-hygiene 是后加的：原来只抓 /physical-health，收进来的全是
        # 疾病向文章（糖尿病/免疫/帕金森/阿尔茨海默），问「怎么提高睡眠质量」
        # 只能命中这些，因为库里根本没有纯睡眠卫生内容。
        # ⚠️ 别用 /sleep-hygiene/healthy-sleep-tips，那个入口带出一大批
        # /best-mattress/ 带货测评（虽有 AFFILIATE_PATH 过滤，但会白占名额）。
        "list_urls": ["https://www.sleepfoundation.org/physical-health",
                      "https://www.sleepfoundation.org/sleep-hygiene"],
        "lang": "en", "category": "recovery", "authority": 0.85,
        "note": "睡眠与恢复，有医学审查",
    },
]


# ------------------------------------------------------------------ 暂不可用（保留记录，别再重复踩）
# 这些源内容质量很高，但抓取时被站点侧硬拦，crawl4ai 会直接返回
# "Blocked by anti-bot protection"。真要用得上代理池或逆向，
# 成本远高于收益，所以先从清单里摘出去。
# 想重新启用：把 entry 加回 SOURCES 并先跑 scripts/probe_sources.py 验证。
# 实测日期 2026-09-14，crawl4ai 0.9.3 + playwright chromium + stealth。
DISABLED_SOURCES: list[dict] = [
    {
        "name": "国家卫生健康委员会", "domain": "nhc.gov.cn",
        "blocked_by": "自建 WAF",
        "tried": ["https://www.nhc.gov.cn/", "http://www.nhc.gov.cn/xcs/kpjy/list.shtml"],
        "note": "两个入口都被拦，健康科普栏目拿不到",
    },
    {
        "name": "NIH PubMed Central", "domain": "ncbi.nlm.nih.gov",
        "blocked_by": "Cloudflare",
        "tried": ["https://www.ncbi.nlm.nih.gov/pmc/?term=exercise+training",
                  "https://pmc.ncbi.nlm.nih.gov/",
                  "https://www.nih.gov/news-events/nih-research-matters"],
        "note": "文献库反爬最严。真要拿论文，走 NCBI E-utilities 官方 API 更稳，"
                "那是另一条路（XML 接口，不是网页爬取）",
    },
    {
        "name": "Mayo Clinic", "domain": "mayoclinic.org",
        "blocked_by": "Akamai",
        "tried": ["https://www.mayoclinic.org/healthy-lifestyle/fitness/basics/health-basics/hlv-20049447",
                  "https://www.mayoclinic.org/healthy-lifestyle/fitness/in-depth/exercise/art-20048389"],
        "note": "明码报 Akamai block，headless 无解",
    },
    {
        "name": "BJSM 英国运动医学杂志", "domain": "bjsm.bmj.com",
        "blocked_by": "Cloudflare",
        "tried": ["https://bjsm.bmj.com/"],
        "note": "BMJ 系全站 Cloudflare。BMJ 有开放获取内容和 RSS，"
                "以后可以直接吃 RSS（还是 XML，不是爬网页）",
    },
    # ---------------- 内容不符（能抓，但没有可用的知识文章）
    # 这些源不是被反爬拦，而是「能抓到、但内容不是运动训练健康知识」。
    # 实测日期 2026-09-14。想重新启用前先跑 scripts/diag_source.py 看抽取结果。
    {
        "name": "国家体育总局", "domain": "sport.gov.cn",
        "blocked_by": "无可用的科普栏目（内容全是政务）",
        "tried": ["https://www.sport.gov.cn/n315/index.html",
                  "https://www.sport.gov.cn/n20001280/index.html"],
        "note": "逐栏目实测：公开/资讯/服务/互动 + 地方动态/总局要闻/通知公告/"
                "政府采购/人事信息/体育数据/规划计划…… 全部是政务公开与工作动态，"
                "没有运动科学科普栏目。要中文训练科学，用下属的体育科学研究所（ciss.cn）",
    },
    {
        "name": "中华医学会", "domain": "cma.org.cn",
        "blocked_by": "无可用的知识栏目（内容全是学会行政）",
        "tried": ["https://www.cma.org.cn/"],
        "note": "/art/ 下是科技奖推荐通知、报废固定资产处置公示、继续医学教育项目"
                "通知这类行政文件。而且页脚「友情链接」的分会名录（运动医疗分会、"
                "肠外肠内营养学分会…）是纯文本清单，会把相关度算到 0.42 骗过闸门——"
                "已用页脚截断 + 新闻噪声过滤挡掉，但源本身没有价值，停用",
    },
    {
        "name": "中国体育报", "domain": "sportspress.cn",
        "blocked_by": "内容不符（体育新闻门户）",
        "tried": ["https://www.sportspress.cn/"],
        "note": "318 条链接全是赛事新闻：NBA/意甲/CBA/德甲比分、球队动态，"
                "属于体育新闻而非训练科学",
    },
    {
        "name": "中国营养学会", "domain": "chinanutri.cn",
        "blocked_by": "内容不符（机构动态）",
        "tried": ["https://www.chinanutri.cn/"],
        "note": "首页为党建活动、工作会议、新春联欢会等机构动态",
    },
    {
        "name": "人民网健康", "domain": "health.people.com.cn",
        "blocked_by": "内容不符（医疗政策新闻）",
        "tried": ["https://health.people.com.cn/",
                  "http://health.people.com.cn/GB/408647"],
        "note": "医疗政策、医院新闻、援外医疗队报道为主，不是可执行的训练/营养依据",
    },
    {
        "name": "中国体育科学学会", "domain": "csss.cn",
        "blocked_by": "内容不符（征文通知）",
        "tried": ["http://www.csss.cn/"],
        "note": "首页是学术会议征文通知、论坛通知等",
    },
    {
        "name": "科普中国", "domain": "kepuchina.cn",
        "blocked_by": "内容不符（综合科普，无体育健康栏目）",
        "tried": ["https://www.kepuchina.cn/"],
        "note": "综合科普平台，首页是平台注册流程、超导材料等非目标主题",
    },
]
