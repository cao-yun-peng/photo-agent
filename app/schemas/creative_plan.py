"""Bounded creative plans; runtime owns resources, sizes and permission."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, StrictBool

ShortText = Annotated[str, Field(min_length=1, max_length=300)]


class PackageOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_mode: Literal["none", "auto", "exact"] = "auto"
    title: str = Field(default="", max_length=60)

    @model_validator(mode="after")
    def check_title(self):
        if self.title_mode == "exact" and not self.title.strip():
            raise ValueError("请填写要使用的标题")
        return self


class CreativePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation: ShortText
    concept: ShortText
    retain: list[ShortText] = Field(min_length=1, max_length=8)
    transform: list[ShortText] = Field(min_length=1, max_length=8)
    discard: list[ShortText] = Field(max_length=8)
    title: str = Field(max_length=60)
    production_prompt: str = Field(min_length=1, max_length=6000)


class VisualReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_preserved: StrictBool
    style_aligned: StrictBool
    title_correct: StrictBool
    no_reference_copy: StrictBool
    issues: list[ShortText] = Field(max_length=8)
    evidence: dict[
        Literal[
            "subject_preserved", "style_aligned", "title_correct", "no_reference_copy"
        ],
        ShortText,
    ] = Field(default_factory=dict)


class ExecutionImage(BaseModel):
    position: int
    role: Literal["subject", "style"]
    path: str
    sha256: str


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    contract: str
    skill_name: str
    skill_version_id: str
    model: str
    size: str
    planner_mode: str
    plan: CreativePlan
    inputs: list[ExecutionImage]
