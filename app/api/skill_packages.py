"""Private package imports and lazy, owner-bound version resources."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.security import get_current_user
from app.database import get_db
from app.models.skill import Skill, SkillAsset, SkillVersion
from app.models.user import User
from app.schemas.skill_package import PackageImportOut, PackageReport, PackageVersionOut
from app.services.skill_package import MAX_ARCHIVE, PackageError, inspect_package

router = APIRouter()
ZIP_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/zip": {"schema": {"type": "string", "format": "binary"}}
        },
    }
}
UserDep = Annotated[User, Depends(get_current_user)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


async def _parse(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_ARCHIVE:
            raise HTTPException(413, "ZIP不得超过16 MiB")
        data.extend(chunk)
    try:
        return await run_in_threadpool(inspect_package, bytes(data))
    except PackageError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/packages/preview", response_model=PackageReport, openapi_extra=ZIP_BODY)
async def preview_package(request: Request, current_user: UserDep):
    return (await _parse(request)).report


@router.post(
    "/packages/import", response_model=PackageImportOut, openapi_extra=ZIP_BODY
)
async def import_package(
    request: Request,
    current_user: UserDep,
    db: DbDep,
    expected_hash: str = Query(pattern=r"^[a-f0-9]{64}$"),
    skill_id: UUID | None = None,
):
    package = await _parse(request)
    report = package.report
    if not report.can_import:
        raise HTTPException(422, "资源不兼容，请先修复预览报告中的问题")
    if report.content_sha256 != expected_hash:
        raise HTTPException(409, "内容已变化，请重新预览")
    # Serialize imports per owner: duplicate retries and limits are transactional.
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update()
    )
    skill = None
    if skill_id:
        skill = (
            await db.execute(
                select(Skill)
                .where(Skill.id == skill_id, Skill.owner_id == current_user.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if skill is None:
            raise HTTPException(404, "未找到你的Skill")
        if skill.kind != "package":
            raise HTTPException(409, "模板Skill不能转换成流程包")
    duplicate_query = (
        select(SkillVersion)
        .join(Skill)
        .where(
            Skill.owner_id == current_user.id,
            SkillVersion.content_sha256 == report.content_sha256,
        )
    )
    if skill_id:
        duplicate_query = duplicate_query.where(SkillVersion.skill_id == skill_id)
    duplicate = (await db.execute(duplicate_query.limit(1))).scalar_one_or_none()
    if duplicate:
        # A retry must not silently reactivate an older version.
        return PackageImportOut(
            skill_id=duplicate.skill_id, version_id=duplicate.id, deduplicated=True
        )
    count = (
        await db.execute(
            select(func.count())
            .select_from(SkillVersion)
            .join(Skill)
            .where(Skill.owner_id == current_user.id)
        )
    ).scalar_one()
    if count >= 100:
        raise HTTPException(409, "最多保存100个流程包版本，请先删除不需要的Skill")
    if skill is None:
        skill = Skill(
            id=uuid4(),
            owner_id=current_user.id,
            name=report.name,
            description=report.description,
            prompt_template="",
            kind="package",
            is_public=False,
            is_official=False,
            reference_keys=[],
        )
        db.add(skill)
        await db.flush()
    version = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        content_sha256=report.content_sha256,
        report=report.model_dump(exclude={"cover_data_url"}),
        instructions=package.instructions,
    )
    db.add(version)
    await db.flush()
    for asset in report.assets:
        db.add(
            SkillAsset(
                version_id=version.id,
                path=asset.path,
                media_type=asset.media_type,
                content=package.files[asset.path],
            )
        )
    skill.current_version_id = version.id
    skill.name, skill.description = report.name, report.description
    await db.commit()
    return PackageImportOut(
        skill_id=skill.id, version_id=version.id, deduplicated=False
    )


async def _owned(db, user_id, skill_id):
    skill = (
        await db.execute(
            select(Skill).where(
                Skill.id == skill_id, Skill.owner_id == user_id, Skill.kind == "package"
            )
        )
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(404, "未找到你的流程包")
    return skill


@router.get("/{skill_id}/versions", response_model=list[PackageVersionOut])
async def list_versions(skill_id: UUID, current_user: UserDep, db: DbDep):
    await _owned(db, current_user.id, skill_id)
    versions = (
        (
            await db.execute(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill_id)
                .order_by(SkillVersion.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        PackageVersionOut(
            id=v.id, skill_id=v.skill_id, report=v.report, instructions=v.instructions
        )
        for v in versions
    ]


@router.get("/{skill_id}/versions/{version_id}/assets")
async def get_asset(
    skill_id: UUID, version_id: UUID, path: str, current_user: UserDep, db: DbDep
):
    await _owned(db, current_user.id, skill_id)
    asset = (
        await db.execute(
            select(SkillAsset)
            .join(SkillVersion)
            .where(
                SkillVersion.skill_id == skill_id,
                SkillAsset.version_id == version_id,
                SkillAsset.path == path,
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "未找到资源")
    return Response(
        asset.content,
        media_type=asset.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
