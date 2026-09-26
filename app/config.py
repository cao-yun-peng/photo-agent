"""全局配置：从 .env 读取，Pydantic 校验."""

from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App
    app_name: str = "photo-agent"
    search_default_timezone: str = "Asia/Shanghai"
    app_env: str = "dev"
    log_level: str = "INFO"
    log_dir: str = ""  # 日志文件目录，空则仅输出到控制台
    log_json_format: bool = False  # dev环境用彩色控制台，生产环境用JSON
    cors_origins: list[str] = Field(default_factory=list)
    admin_enabled: bool = False
    admin_user_ids: list[UUID] = Field(default_factory=list)

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

    # Web accounts: registration can be closed without disabling existing users.
    web_registration_enabled: bool = True

    # WeChat MiniProgram
    wechat_appid: str = ""
    wechat_secret: str = ""

    # OSS
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_key_id: str = ""
    oss_key_secret: str = ""
    oss_upload_ttl: int = 900
    oss_backend: Literal["auto", "oss", "mock"] = "auto"
    mock_oss_enabled: bool = False
    mock_oss_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

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
    photo_processing_lease_seconds: int = Field(default=60, ge=10)
    photo_processing_max_attempts: int = Field(default=3, ge=1)
    photo_recovery_batch_size: int = Field(default=100, ge=1, le=1000)

    # 照片搜索索引补算：总共 5 次实际 embedding 调用（首次 + 4 次重试）。
    # 每个延迟都从“上一次实际调用失败结束”后开始计算。
    embedding_max_attempts: int = 5
    embedding_retry_delays_seconds: list[int] = [2, 8, 25, 60]

    # Top-K 查询-候选判同重排。模型为空时复用 qwen_chat_model。
    search_cache_revision: str = "1"
    search_ann_enabled: bool = False
    search_ann_ef_search: int = Field(default=200, ge=40, le=1000)
    search_ann_max_scan_tuples: int = Field(default=20000, ge=1000, le=100000)
    search_snapshot_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    search_snapshot_max_candidates: int = Field(default=300, ge=10, le=5000)
    # Per external search request/job; idle time between pages is not charged.
    # Model/candidate/cost-unit quotas below remain shared across the whole plan.
    search_total_timeout_seconds: float = Field(default=90.0, ge=1, le=180)
    search_max_model_calls: int = Field(default=20, ge=0, le=100)
    search_max_visual_calls: int = Field(default=3, ge=0, le=20)
    search_max_verified_candidates: int = Field(default=60, ge=0, le=500)
    search_max_budget_units: int = Field(default=60, ge=0, le=1000)
    search_rerank_enabled: bool = True
    search_rerank_model: str = ""
    search_rerank_top_k: int = 5
    search_rerank_reject_confidence: float = 0.8
    search_rerank_require_match: bool = True
    # Browse pages may contain fewer than limit: return the first verified batch
    # with a continuation cursor instead of spending the deadline filling a page.
    search_browse_early_return: bool = True
    search_rerank_timeout_seconds: float = 45.0
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
    # Foreground search execution only; dispatch adds cleanup time separately.
    agent_search_turn_budget_seconds: float = Field(default=90.0, gt=0, le=180)
    # 仅后台预取会启用强制视觉兜底；30 秒兼顾弱文本描述召回与 180 秒 Worker 总预算。
    agent_search_visual_budget_seconds: float = 30.0
    agent_search_prefetch_wait_seconds: float = 2.0
    agent_search_pool_ttl_seconds: int = 10 * 60

    # OpenAI (可选，用于 gpt-image-2 / Agent function calling)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_image_transport: str = Field(default="sync", pattern="^(sync|timicc_async)$")

    # 生图限流：每人每天免费额度
    gen_daily_free_quota: int = 3
    generation_confirmation_ttl_seconds: int = 10 * 60
    generation_estimated_cost_yuan: float = 0.14
    generation_lease_seconds: int = Field(default=300, ge=240, le=3600)
    generation_max_iterations: int = Field(default=2, ge=0, le=5)
    generation_chain_estimate_limit_yuan: float = Field(default=1.0, gt=0)
    package_generation_estimated_cost_yuan: float = Field(default=0.30, gt=0)
    provider_price_version: str = Field(
        default="unverified-config", min_length=1, max_length=128
    )
    vl_input_yuan_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    vl_output_yuan_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )

    # Agent v2 灰度：稳定按 user_id 分桶；kill switch 优先级最高。
    agent_v2_enabled: bool = False
    agent_v2_rollout_percent: int = 0
    agent_v2_rollout_salt: str = "photo-agent-v2"
    agent_v2_kill_switch: bool = False

    # Agent 并发锁 TTL（秒）
    agent_lock_ttl: int = 30
    task_cleanup_timeout_seconds: float = Field(default=5.0, gt=0)

    def uses_mock_oss(self) -> bool:
        if self.oss_backend != "auto":
            return self.oss_backend == "mock"
        return (
            not self.oss_bucket
            or self.oss_bucket == "photo-agent-dev"
            or self.oss_key_id in ("", "LTAI_xxx")
        )

    def validate_runtime(self) -> None:
        """Called by both API and Worker before accepting work; no secret values in errors."""
        from zoneinfo import ZoneInfo

        ZoneInfo(self.search_default_timezone)
        if self.uses_mock_oss():
            if self.app_env not in {"dev", "test"} or not self.mock_oss_enabled:
                raise ValueError("Mock OSS requires dev/test and MOCK_OSS_ENABLED=true")
        elif not all(
            (self.oss_endpoint, self.oss_bucket, self.oss_key_id, self.oss_key_secret)
        ):
            raise ValueError("Real OSS configuration is incomplete")
        if self.admin_enabled and not self.admin_user_ids:
            raise ValueError("ADMIN_ENABLED requires ADMIN_USER_IDS")
        if self.app_env not in {"dev", "test"}:
            for key in (self.dashscope_api_key, self.openai_api_key):
                if not key or key.strip() in {"", "sk-xxx", "sk-openai-xxx"}:
                    raise ValueError(
                        "Production requires configured chat/VL and image model credentials"
                    )
            if self.mock_oss_enabled or self.oss_key_id == "LTAI_xxx":
                raise ValueError("Mock configuration is forbidden outside dev/test")
            if (
                len(self.jwt_secret) < 32
                or "change_me" in self.jwt_secret
                or self.jwt_secret.startswith("change")
            ):
                raise ValueError("Production requires a strong JWT secret")

    # Agent 循环预算（P0-1: 时间/Token/费用预算）
    agent_max_time_seconds: int = 150
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
