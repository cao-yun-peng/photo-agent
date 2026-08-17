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

    # 检索判别属性：只记录画面中可见、可验证的信息
    actions: list[str] = Field(default_factory=list, description="人物或主体正在进行的具体动作")
    age_groups: list[str] = Field(default_factory=list, description="可辨认的年龄组，如儿童/青年/中年/老年")
    blur_type: str | None = Field(default=None, description="模糊类型，如运动模糊/失焦/相机抖动/隔窗模糊")
    capture_context: list[str] = Field(default_factory=list, description="拍摄载体或视角，如公交车内/隔窗拍摄/自拍")
    spatial_layout: list[str] = Field(default_factory=list, description="主体位置、朝向和相对关系")
    distinctive_details: list[str] = Field(default_factory=list, description="可区分近似照片的视觉细节")

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
    analysis_version: str = Field(default="v4", description="结构化分析 Prompt/Schema 版本")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scene": "户外",
                "scene_detail": "海边沙滩",
                "persons": {"count": 2, "age_estimate": "青年", "expression": "微笑"},
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
                "analysis_version": "v4",
            }
        }
    )
