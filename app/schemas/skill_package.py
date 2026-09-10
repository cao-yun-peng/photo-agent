"""Import reports are data; package instructions never become server permissions."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PackageAssetInfo(BaseModel):
    path: str
    media_type: str
    size: int
    sha256: str


class PackageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    importer_version: str = "skill-zip-v1"
    name: str
    description: str
    root: str
    content_sha256: str
    source: str | None = None
    license: str | None = None
    assets: list[PackageAssetInfo]
    references: dict[str, list[str]]
    cover_path: str | None = None
    cover_data_url: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    supported: list[str] = Field(
        default_factory=lambda: [
            "Markdown说明",
            "本地参考文档",
            "PNG/JPEG/WebP资源",
            "私有版本保存",
        ]
    )
    execution_status: Literal["not_available", "planning_available"] = "planning_available"
    can_import: bool = True


class PackageVersionOut(BaseModel):
    id: UUID
    skill_id: UUID
    report: PackageReport
    instructions: str


class PackageImportOut(BaseModel):
    skill_id: UUID
    version_id: UUID
    deduplicated: bool
