"""照片相关 schema."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UploadUrlRequest(BaseModel):
    """请求 OSS 直传签名。hash 用于去重。"""

    hash: str = Field(..., min_length=64, max_length=64, description="SHA-256")
    size_bytes: int = Field(..., gt=0, le=100 * 1024 * 1024)  # 100MB 上限
    mime_type: str = Field(default="image/jpeg", max_length=64)


class UploadUrlResponse(BaseModel):
    upload_url: str
    oss_key: str
    headers: dict[str, str]
    method: str = "PUT"
    expires_in: int
    duplicate: bool = False


class PhotoCreate(BaseModel):
    oss_key: str
    hash: str = Field(..., min_length=64, max_length=64)
    size_bytes: int
    mime_type: str = "image/jpeg"


class PhotoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    oss_key: str
    thumb_key: str | None
    taken_at: datetime | None
    ai_description: str | None
    status: str
    search_index_status: str
    search_index_message: str
    embedding_retry_count: int = 0
    embedding_max_attempts: int = 5
    embedding_next_retry_at: datetime | None = None
    created_at: datetime


class PhotoListItem(BaseModel):
    """时间线返回项，附带临时缩略图 URL."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    thumb_url: str | None = None
    taken_at: datetime | None
    ai_description: str | None
    status: str
    search_index_status: str
    search_index_message: str
    embedding_retry_count: int = 0
    embedding_max_attempts: int = 5
    embedding_next_retry_at: datetime | None = None


class PhotoProcessingStatus(BaseModel):
    photo_id: UUID
    photo_status: str
    search_index_status: str
    retry_count: int = 0
    max_attempts: int = 5
    next_retry_at: datetime | None = None
    next_retry_in_seconds: int | None = None
    message: str


class PhotoProcessingStatusBatchRequest(BaseModel):
    photo_ids: list[UUID] = Field(..., min_length=1, max_length=100)


class PhotoProcessingStatusBatchResponse(BaseModel):
    items: list[PhotoProcessingStatus]


# ------------------------------------------------------------------
# D8–D9 搜索
# ------------------------------------------------------------------
class SearchQuery(BaseModel):
    """支持多维过滤 + 混合排序 + 游标分页。

    示例：
      {"q":"雨天的猫", "from_date":"2026-07-01", "to_date":"2026-07-31",
       "tags":["小橘"], "limit":20}
    """

    q: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=10000)
    # browse=最多 5 张；best=系统选最佳 1 张；select=由用户选择，不设业务硬上限
    result_mode: Literal["browse", "best", "select"] = "browse"
    complete_result_set: bool = Field(
        default=False,
        description="select 模式下扫描并返回全部符合硬条件的已索引照片",
    )

    # 过滤维度
    from_date: date | None = None
    to_date: date | None = None
    tags: list[str] | None = None  # 命中任一即可（OR）
    status: str | None = Field(default="done")  # 一般只搜处理完的

    # 结构化分析 JSONB 过滤（OR 语义）
    scene: str | None = None  # 场景大类
    objects: list[str] | None = None  # 物体标签
    text_in_image: list[str] | None = None  # 图中文字
    mood: str | None = None  # 氛围
    colors: list[str] | None = None  # 主色调
    photo_types: (
        list[
            Literal[
                "selfie",
                "screenshot",
                "group_photo",
                "portrait",
                "document",
                "food",
                "scenery",
                "other",
            ]
        ]
        | None
    ) = None
    is_selfie: bool | None = None
    people_count_min: int | None = Field(default=None, ge=0)
    people_count_max: int | None = Field(default=None, ge=0)

    # 可选的语义相似度硬阈值。None 使用服务端配置；0 显式关闭。
    min_semantic_score: float | None = Field(default=None, ge=0, le=1)

    # 排序权重（0–1），加起来不必等于 1，程序会归一
    w_semantic: float = Field(default=0.7, ge=0, le=1)
    w_recency: float = Field(default=0.2, ge=0, le=1)
    w_interaction: float = Field(default=0.1, ge=0, le=1)

    # 游标：上一页最后一张的复合游标（sort_score:photo_id）
    cursor: str | None = None

    # 是否让服务器帮我把自然语言解析成结构化条件
    auto_parse: bool = False

    # 对可见文字/品牌/数值/日期/路线等强约束做候选证据校验
    verify_constraints: bool = True

    # 对当前页前 K 个候选执行查询-候选判同；可显式关闭以运行基线对照
    verify_semantic: bool = True

    @model_validator(mode="after")
    def validate_complete_result_mode(self) -> "SearchQuery":
        if self.complete_result_set and self.result_mode != "select":
            raise ValueError("complete_result_set 仅支持 select 模式")
        if (
            self.people_count_min is not None
            and self.people_count_max is not None
            and self.people_count_min > self.people_count_max
        ):
            raise ValueError("people_count_min 不能大于 people_count_max")
        return self


class SearchResultItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    thumb_url: str | None = None
    taken_at: datetime | None = None
    ai_description: str | None = None
    status: str
    # 分数拆解，前端可选择显示
    score_semantic: float = 0.0
    score_recency: float = 0.0
    score_interaction: float = 0.0
    score_final: float = 0.0


class ParsedQuery(BaseModel):
    """query_parser 拆解后的结构化条件。留白位可以让 auto_parse=True 时生效。"""

    semantic: str
    from_date: date | None = None
    to_date: date | None = None
    place: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchConstraintCheck(BaseModel):
    """强约束候选校验摘要；不暴露被过滤照片的内容。"""

    applied: bool = True
    constraints: list[dict[str, str]] = Field(default_factory=list)
    candidates_checked: int = 0
    matched_count: int = 0
    rejected_count: int = 0
    rejected_by_kind: dict[str, int] = Field(default_factory=dict)


class SearchRerankCheck(BaseModel):
    """Top-K 判同摘要；不返回被过滤候选的 ID 或图片内容。"""

    applied: bool = True
    degraded: bool = False
    degraded_reason: str | None = None
    prompt_version: str
    model: str | None = None
    candidates_checked: int = 0
    match_count: int = 0
    uncertain_count: int = 0
    contradiction_count: int = 0
    rejected_count: int = 0
    zero_match_filtered: bool = False
    unjudged_filtered_count: int = 0
    cache_hit: bool = False
    latency_ms: float = 0.0
    visual_verification_applied: bool = False
    visual_prompt_version: str | None = None
    visual_trigger_reason: str | None = None
    visual_candidates_checked: int = 0
    visual_match_count: int = 0
    visual_uncertain_count: int = 0
    visual_contradiction_count: int = 0
    visual_cache_hit: bool = False
    visual_degraded: bool = False
    visual_degraded_reason: str | None = None
    visual_latency_ms: float = 0.0


class SearchIndexCoverage(BaseModel):
    total_photos: int = 0
    indexed_photos: int = 0
    retrying_photos: int = 0
    unavailable_photos: int = 0
    coverage_ratio: float = 1.0
    message: str | None = None
    faceted_photos: int = 0
    facet_coverage_ratio: float = 1.0
    semantic_complete: bool = True
    semantic_message: str | None = None


class SearchResult(BaseModel):
    items: list[SearchResultItem]
    total: int
    result_mode: Literal["browse", "best", "select"] = "browse"
    total_matches: int = 0
    result_set_complete: bool = False
    completeness_reason: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    parsed: ParsedQuery | None = None  # 若 auto_parse=True，返回解析结果给前端展示
    cache_hit: bool = False  # embedding 是否命中缓存
    constraint_check: SearchConstraintCheck | None = None
    rerank_check: SearchRerankCheck | None = None
    index_coverage: SearchIndexCoverage | None = None
    similarity_threshold: float | None = None
    threshold_filtered_count: int = 0
    threshold_bypassed_reason: str | None = None
    coverage_hint: str | None = None
    semantic_facets_required: bool = False


class AlbumFallbackQuery(BaseModel):
    """智能全量相册兜底查询：无严格过滤，按语义+新鲜度+个性化综合排序。"""

    q: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None

    w_semantic: float = Field(default=0.4, ge=0, le=1)
    w_recency: float = Field(default=0.35, ge=0, le=1)
    w_interaction: float = Field(default=0.25, ge=0, le=1)


class SearchClick(BaseModel):
    """前端上报用户点击了某条搜索结果。"""

    photo_id: UUID
    query: str = Field(default="", max_length=200)
    rank: int = Field(default=0, ge=0)


class PhotoInteract(BaseModel):
    """前端上报用户与单张照片的交互行为。"""

    action: str = Field(..., pattern="^(view|favorite|share|download)$")
    context: str | None = Field(default=None, max_length=64)
    # context: 交互来源，如 search_result / timeline / generation_source
