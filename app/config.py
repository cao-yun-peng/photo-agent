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

    # OpenTelemetry：默认关闭，避免本地/测试环境依赖 Collector。
    # 生产环境通过 OTEL_ENABLED=true 开启 Trace + Log OTLP 导出。
    otel_enabled: bool = False
    otel_service_name: str = ""
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4318"
    otel_trace_sample_ratio: float = 1.0
    otel_export_logs: bool = True
    otel_capture_content: bool = False
    otel_excluded_urls: str = "/live,/health,/ready,/docs,/openapi.json"

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

    # 照片搜索索引补算：总共 5 次实际 embedding 调用（首次 + 4 次重试）。
    # 每个延迟都从“上一次实际调用失败结束”后开始计算。
    embedding_max_attempts: int = 5
    embedding_retry_delays_seconds: list[int] = [2, 8, 25, 60]

    # Top-K 查询-候选判同重排。模型为空时复用 qwen_chat_model。
    search_rerank_enabled: bool = True
    search_rerank_model: str = ""
    search_rerank_top_k: int = 5
    search_rerank_reject_confidence: float = 0.8
    search_rerank_require_match: bool = True
    search_rerank_timeout_seconds: float = 12.0
    search_rerank_cache_ttl_seconds: int = 7 * 24 * 3600
    # 0 表示默认关闭全局相似度硬阈值。现有离线评测显示单一阈值会明显
    # 牺牲召回率；可按环境标定后设置 0~1，或由搜索请求显式传入。
    search_semantic_min_score: float = 0.0

    # 二次视觉判定默认关闭；仅在完成 development/validation 对照后显式开启。
    search_visual_verify_enabled: bool = False
    search_visual_verify_top_k: int = 3
    search_visual_verify_score_gap: float = 0.05
    search_visual_verify_timeout_seconds: float = 45.0
    search_visual_verify_cache_ttl_seconds: int = 7 * 24 * 3600
    search_visual_verify_image_url_ttl_seconds: int = 300

    # Agent 多轮续搜：首次搜索后预取一批明确匹配的候选，追问优先从池中取。
    agent_search_candidate_pool_size: int = 12
    agent_search_visual_fallback: bool = True
    agent_search_auto_repair_index: bool = True
    agent_search_index_repair_limit: int = 10
    agent_search_turn_budget_seconds: float = 12.0
    # 仅后台预取会启用强制视觉兜底；30 秒兼顾弱文本描述召回与 180 秒 Worker 总预算。
    agent_search_visual_budget_seconds: float = 30.0
    agent_search_prefetch_wait_seconds: float = 2.0
    agent_search_pool_ttl_seconds: int = 10 * 60

    # OpenAI (可选，用于 gpt-image-2 / Agent function calling)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # 生图限流：每人每天免费额度
    gen_daily_free_quota: int = 3
    generation_confirmation_ttl_seconds: int = 10 * 60
    generation_estimated_cost_yuan: float = 0.14

    # Agent v2 灰度：稳定按 user_id 分桶；kill switch 优先级最高。
    agent_v2_enabled: bool = False
    agent_v2_rollout_percent: int = 0
    agent_v2_rollout_salt: str = "photo-agent-v2"
    agent_v2_kill_switch: bool = False

    # Agent 并发锁 TTL（秒）
    agent_lock_ttl: int = 30

    # Agent 循环预算（P0-1: 时间/Token/费用预算）
    agent_max_time_seconds: int = 60
    # 真实 Agent 每步约消耗 1.8k Token；8 步链路累计预算留到 20k。
    agent_max_total_tokens: int = 20000
    agent_max_cost_yuan: float = 1.0
    # Agent 单工具执行超时（P0-2: 工具执行超时保护）
    agent_tool_timeout: int = 15

    # 熔断器配置（秒）
    cb_failure_threshold: int = 3
    cb_vl_recovery_interval: int = 300
    cb_embedding_recovery_interval: int = 30
    cb_chat_recovery_interval: int = 120
    cb_search_rerank_recovery_interval: int = 120
    cb_search_visual_verify_recovery_interval: int = 180
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
