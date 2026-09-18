from pathlib import Path
from pydantic_settings import BaseSettings

# backend/app/config.py -> backend/ -> 项目根
BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = ROOT_DIR / "data"


class Settings(BaseSettings):
    # 默认 SQLite，零依赖；换 PostgreSQL 只改这一行即可
    DATABASE_URL: str = f"sqlite:///{ (DATA_DIR / 'fitness.db').as_posix() }"
    CHECKPOINT_DB: str = str(DATA_DIR / "checkpoints.db")
    UPLOAD_DIR: str = str(DATA_DIR / "uploads")

    # LLM：DeepSeek 兼容 OpenAI 协议
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.4

    # 博查搜索：关键词爬取时按关键词发现全网新源（open.bochaai.com 注册）
    BOCHA_API_KEY: str = ""

    # 分析参数
    DEFAULT_MAX_HR: int = 190
    DEFAULT_RESTING_HR: int = 60

    # 时区：华为导出的 GPX/TCX 时间戳带 Z（UTC），入库前按这个偏移换算成本地时间。
    # 中国自 1991 年起没有夏令时，固定 +8 是精确的。换时区只改这一行。
    LOCAL_UTC_OFFSET_HOURS: float = 8.0

    class Config:
        env_file = str(BACKEND_DIR / ".env")
        extra = "ignore"


settings = Settings()

DATA_DIR.mkdir(parents=True, exist_ok=True)
Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
