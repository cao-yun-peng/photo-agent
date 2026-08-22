"""VL 结构化分析结果的 Schema."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PhotoType = Literal[
    "selfie",
    "screenshot",
    "group_photo",
    "portrait",
    "document",
    "food",
    "scenery",
    "other",
]
PHOTO_TYPES = frozenset(PhotoType.__args__)


def infer_semantic_facets(data: dict[str, Any]) -> dict[str, Any]:
    """从 v4/v5 分析结果推导稳定的集合检索字段。

    v5 模型输出优先；自拍和截图属于可由旧 ``capture_context`` 高置信度
    纠正的特殊类型。旧数据缺少 photo_type 时才使用人数和场景做兼容推导。
    """

    persons = data.get("persons")
    persons = persons if isinstance(persons, dict) else {}
    raw_count = persons.get("count", data.get("people_count", 0))
    try:
        people_count = max(0, int(raw_count or 0))
    except (TypeError, ValueError):
        people_count = 0

    contexts = data.get("capture_context")
    contexts = contexts if isinstance(contexts, list) else []
    context_text = " ".join(str(value) for value in contexts)
    raw_selfie = data.get("is_selfie")
    is_selfie = bool(raw_selfie) if isinstance(raw_selfie, bool) else False
    is_selfie = is_selfie or "自拍" in context_text

    raw_type = str(data.get("photo_type") or "").strip()
    photo_type = raw_type if raw_type in PHOTO_TYPES else ""
    if is_selfie:
        photo_type = "selfie"
    elif "截图" in context_text or "屏幕截图" in context_text:
        photo_type = "screenshot"
    elif not photo_type:
        scene = str(data.get("scene") or "")
        objects = " ".join(str(value) for value in data.get("objects") or [])
        if people_count >= 2:
            photo_type = "group_photo"
        elif people_count == 1:
            photo_type = "portrait"
        elif any(
            word in f"{scene} {objects}" for word in ("文档", "票据", "证件", "书页")
        ):
            photo_type = "document"
        elif any(
            word in f"{scene} {objects}" for word in ("餐厅", "食物", "菜", "饮品")
        ):
            photo_type = "food"
        elif scene in {"户外", "公园", "景区", "海边", "沙滩", "街道"}:
            photo_type = "scenery"
        else:
            photo_type = "other"

    return {
        "photo_type": photo_type,
        "is_selfie": is_selfie,
        "people_count": people_count,
    }


class PersonInfo(BaseModel):
    """照片中的人物信息."""

    count: int = Field(default=0, ge=0, description="人数")
    age_estimate: str | None = Field(
        default=None, description="年龄段估计，如儿童/青年/中年/老年"
    )
    expression: str | None = Field(default=None, description="表情或动作描述")


class ImageAnalysis(BaseModel):
    """单张照片的结构化分析结果。

    字段尽量保持扁平，方便存进 JSONB 并在 SQL 中做过滤。
    """

    # 场景：大类 + 细节
    scene: str = Field(
        default="unknown", description="场景大类：室内/户外/餐厅/景区/街道/居家等"
    )
    scene_detail: str | None = Field(default=None, description="更具体的场景描述")

    # 人物
    persons: PersonInfo = Field(default_factory=PersonInfo)
    photo_type: PhotoType = Field(default="other", description="照片类型受控标签")
    is_selfie: bool = Field(default=False, description="是否为自拍")
    people_count: int = Field(default=0, ge=0, description="可辨认的真实人物数量")

    # 检索判别属性：只记录画面中可见、可验证的信息
    actions: list[str] = Field(
        default_factory=list, description="人物或主体正在进行的具体动作"
    )
    age_groups: list[str] = Field(
        default_factory=list, description="可辨认的年龄组，如儿童/青年/中年/老年"
    )
    blur_type: str | None = Field(
        default=None, description="模糊类型，如运动模糊/失焦/相机抖动/隔窗模糊"
    )
    capture_context: list[str] = Field(
        default_factory=list, description="拍摄载体或视角，如公交车内/隔窗拍摄/自拍"
    )
    spatial_layout: list[str] = Field(
        default_factory=list, description="主体位置、朝向和相对关系"
    )
    distinctive_details: list[str] = Field(
        default_factory=list, description="可区分近似照片的视觉细节"
    )

    # 物体：用标签列表，便于搜索过滤
    objects: list[str] = Field(
        default_factory=list, description="照片中显著物体，如食物、宠物、建筑"
    )

    # 图中文字
    text_in_image: list[str] = Field(
        default_factory=list, description="图中识别到的文字片段"
    )

    # 氛围/情绪
    mood: str | None = Field(
        default=None, description="氛围词：温馨、热闹、安静、正式等"
    )

    # 主色调
    colors: list[str] = Field(
        default_factory=list, description="主色调，如 ['蓝色', '白色']"
    )

    # 一句话摘要（用于展示和兜底 embedding）
    summary: str = Field(default="", description="综合一句话摘要")

    # 解析质量标记（程序写入，非 VL 输出）
    parse_quality: str = Field(
        default="ok", description="解析质量：ok / fallback / empty"
    )
    analysis_version: str = Field(
        default="v5", description="结构化分析 Prompt/Schema 版本"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_semantic_facets(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        facets = infer_semantic_facets(normalized)
        normalized.update(facets)
        persons = normalized.get("persons")
        persons = dict(persons) if isinstance(persons, dict) else {}
        persons["count"] = facets["people_count"]
        normalized["persons"] = persons
        return normalized

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scene": "户外",
                "scene_detail": "海边沙滩",
                "persons": {"count": 2, "age_estimate": "青年", "expression": "微笑"},
                "photo_type": "group_photo",
                "is_selfie": False,
                "people_count": 2,
                "actions": ["并排站立", "看向镜头"],
                "age_groups": ["青年"],
                "blur_type": "无明显模糊",
                "capture_context": ["正面拍摄"],
                "spatial_layout": ["两人位于画面中央", "海浪位于人物后方"],
                "distinctive_details": ["右侧人物戴白色遮阳帽"],
                "objects": ["沙滩", "海浪", "太阳伞"],
                "text_in_image": [],
                "mood": "轻松愉快",
                "colors": ["蓝色", "金色"],
                "summary": "两名青年在海边沙滩并排站立并看向镜头，右侧人物戴白色遮阳帽，氛围轻松愉快。",
                "parse_quality": "ok",
                "analysis_version": "v5",
            }
        }
    )
