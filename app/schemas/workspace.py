from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(default="", max_length=200)
    target_count: int = Field(default=12, ge=1, le=100)
    min_group_count: int = Field(default=0, ge=0, le=100)
    prefer_landscape: bool = False
    locked_ids: list[UUID] = Field(default_factory=list, max_length=100)
    excluded_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def limits(self):
        if (
            self.min_group_count > self.target_count
            or len(set(self.locked_ids)) > self.target_count
        ):
            raise ValueError("constraints exceed target count")
        if set(self.locked_ids) & set(self.excluded_ids):
            raise ValueError("locked photos cannot be excluded")
        return self


class ExplicitPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_subject: Literal["none", "people", "landscape"] = "none"
    title_mode: Literal["auto", "none"] = "auto"


class WorkspaceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "add_selection",
        "remove_selection",
        "clear_selection",
        "set_task",
        "set_preferences",
        "set_fact",
        "clear_fact",
        "clear_facts",
        "curate",
        "save_album",
        "load_album",
        "delete_album",
    ]
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    photo_ids: list[UUID] = Field(default_factory=list, max_length=100)
    album_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=80)
    task: TaskMemory | None = None
    preferences: ExplicitPreferences | None = None
    photo_version: str | None = Field(default=None, min_length=64, max_length=64)
    fact: str | None = Field(default=None, min_length=1, max_length=160)


class UndoCommand(BaseModel):
    expected_revision: int = Field(ge=0)


class WorkspacePhoto(BaseModel):
    id: UUID
    version: str
    thumb_url: str | None = None
    description: str | None = None
    correction: dict | None = None
    correction_stale: bool = False


class AlbumSummary(BaseModel):
    id: UUID
    title: str
    count: int


class UndoInfo(BaseModel):
    operation_id: UUID
    expires_at: str


class WorkspaceOut(BaseModel):
    revision: int
    selection: list[WorkspacePhoto]
    task: TaskMemory
    preferences: ExplicitPreferences
    memory_source: str
    updated_at: str | None = None
    task_updated_at: str | None = None
    preferences_updated_at: str | None = None
    albums: list[AlbumSummary]
    missing_selection_count: int
    undo: UndoInfo | None = None
    selection_report: dict | None = None
    facts: list[dict] = Field(default_factory=list)


class ActionOut(BaseModel):
    workspace: WorkspaceOut
    operation_id: UUID
    can_undo: bool
    report: dict | None = None
