"""用户相关 schema."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    nickname: str | None
    avatar_url: str | None
    created_at: datetime
