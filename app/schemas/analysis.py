"""VL 结构化分析结果的 Schema."""
from pydantic import BaseModel, ConfigDict, Field


class PersonInfo(BaseModel):
    """照片中的人物信息."""

    count: int = Field(default=0, ge=0, description="人数")
    age_estimate: str | None = Field(default=None, description="年龄段估计，如儿童/青年/中年/老年")
    expression: str | None = Field(default=None, description="表情或动作描述")


class ImageAnalysis(BaseModel):
    """单张照片的结构化分析结果。

    字段尽量保持扁平，方便存进 JSONB 并在 SQL 中做过滤。
    """

    # 场景：大类 + 细节
    scene: str = Field(default="unknown", description="场景大类：室内/户外/餐厅/景区/街道/居家等")
    scene_detail: str | None = Field(default=None, description="更具体的场景描述")

    # 人物
    persons: PersonInfo = Field(default_factory=PersonInfo)

    # 物体：用标签列表，便于搜索过滤
    objects: list[str] = Field(default_factory=list, description="照片中显著物体，如食物、宠物、建筑")

    # 图中文字
    text_in_image: list[str] = Field(default_factory=list, description="图中识别到的文字片段")

    # 氛围/情绪
    mood: str | None = Field(default=None, description="氛围词：温馨、热闹、安静、正式等")

    # 主色调
    colors: list[str] = Field(default_factory=list, description="主色调，如 ['蓝色', '白色']")

    # 一句话摘要（用于展示和兜底 embedding）
    summary: str = Field(default="", description="综合一句话摘要")

    # 解析质量标记（程序写入，非 VL 输出）
    parse_quality: str = Field(default="ok", description="解析质量：ok / fallback / empty")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scene": "户外",
                "scene_detail": "海边沙滩",
                "persons": {"count": 2, "age_estimate": "青年", "expression": "微笑"},
                "objects": ["沙滩", "海浪", "太阳伞"],
                "text_in_image": [],
                "mood": "轻松愉快",
                "colors": ["蓝色", "金色"],
                "summary": "两张青年在海边沙滩微笑的照片，氛围轻松愉快。",
                "parse_quality": "ok",
            }
        }
    )
