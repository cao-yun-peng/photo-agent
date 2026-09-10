from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.workspace import WorkspaceCommand, UndoCommand, WorkspaceOut, ActionOut
from app.services import photo_workspace as service
from app.services.generation_service import GenerationDomainError

router = APIRouter(prefix="/workspace", tags=["workspace"])
UserDep = Annotated[User, Depends(get_current_user)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


def failure(exc):
    raise HTTPException(
        exc.status_code, detail={"code": exc.code, "message": str(exc)}
    ) from exc


@router.get("", response_model=WorkspaceOut)
async def get_workspace(user: UserDep, db: DbDep):
    return await service.read_workspace(db, user.id)


@router.post("/actions", response_model=ActionOut)
async def apply(payload: WorkspaceCommand, user: UserDep, db: DbDep):
    try:
        return await service.apply_command(db, user.id, payload)
    except GenerationDomainError as exc:
        failure(exc)


@router.post("/actions/{operation_id}/undo", response_model=ActionOut)
async def undo(operation_id: UUID, payload: UndoCommand, user: UserDep, db: DbDep):
    try:
        return await service.undo_command(
            db, user.id, operation_id, payload.expected_revision
        )
    except GenerationDomainError as exc:
        failure(exc)


@router.get("/albums/{album_id}")
async def get_album(album_id: UUID, user: UserDep, db: DbDep):
    try:
        album, ids = await service.get_album(db, user.id, album_id)
        return {
            "id": str(album.id),
            "title": album.title,
            "photos": [
                service.photo_out(p, {}) for p in await service.photos(db, user.id, ids)
            ],
        }
    except GenerationDomainError as exc:
        failure(exc)
