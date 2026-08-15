"""全局配置：从 .env 读取，Pydantic 校验."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App
    app_name: str = "photo-agent"
    app_env: str = "dev"
    log_level: str = "INFO"
    log_dir: str = ""  # 日志文件目录，空则仅输出到控制台
    log_json_format: bool = False  # dev环境用彩色控制台，生产环境用JSON

    # DB
    database_url: str

    # Redis
    redis_url: str

    # JWT
    jwt_secret: str
    jwt_expire_minutes: int = 10080  # 7 天
    jwt_algorithm: str = "HS256"

    # WeChat MiniProgram
    wechat_appid: str = ""
    wechat_secret: str = ""

    # OSS
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_key_id: str = ""
    oss_key_secret: str = ""
    oss_upload_ttl: int = 900

    # DashScope
    dashscope_api_key: str = ""
    dashscope_chat_url: str = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    qwen_vl_model: str = "qwen-vl-plus"
    qwen_embedding_model: str = "text-embedding-v3"
    # Agent 决策用的文本模型（支持 function calling）
    qwen_chat_model: str = "qwen-plus"

    # Worker concurrency. Image understanding performs multiple outbound model
    # calls per job, so a conservative default avoids connection bursts.
    worker_max_jobs: int = 4

    # Top-K 查询-候选判同重排。模型为空时复用 qwen_chat_model。
    search_rerank_enabled: bool = True
    search_rerank_model: str = ""
    search_rerank_top_k: int = 5
    search_rerank_reject_confidence: float = 0.8
    search_rerank_require_match: bool = True
    search_rerank_timeout_seconds: float = 12.0
    search_rerank_cache_ttl_seconds: int = 7 * 24 * 3600

    # OpenAI (可选，用于 gpt-image-2 / Agent function calling)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # 生图限流：每人每天免费额度
    gen_daily_free_quota: int = 3

    # Agent 并发锁 TTL（秒）
    agent_lock_ttl: int = 30

    # Agent 循环预算（P0-1: 时间/Token/费用预算）
    agent_max_time_seconds: int = 60
    agent_max_total_tokens: int = 8000
    agent_max_cost_yuan: float = 1.0
    # Agent 单工具执行超时（P0-2: 工具执行超时保护）
    agent_tool_timeout: int = 15

    # 熔断器配置（秒）
    cb_failure_threshold: int = 3
    cb_vl_recovery_interval: int = 300
    cb_embedding_recovery_interval: int = 300
    cb_chat_recovery_interval: int = 120
    cb_search_rerank_recovery_interval: int = 120
    cb_image_gen_recovery_interval: int = 300
    cb_oss_recovery_interval: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """单例读取配置。lru_cache 保证只解析一次 .env。"""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
