"""知识库：每周爬取权威运动训练健康文章，结构化入库，供教练与计划生成引用。

刻意不在包 __init__ 里 import 依赖数据库的模块（store / retrieve），
避免 app.database 在初始化时反向 import 本包造成循环。
"""
from app.knowledge.scoring import combined_score  # noqa: F401
from app.knowledge.sources import SOURCES, domain_score, suffix_weight  # noqa: F401

__all__ = ["SOURCES", "domain_score", "suffix_weight", "combined_score"]
