"""Photos 路由：签名、回调、列表、详情、删除."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.photo import Photo
from app.models.user import User
from app.schemas.photo import (
    PhotoCreate,
    PhotoInteract,
    PhotoListItem,
    PhotoOut,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.circuit_breaker import ServiceDegradedError
from app.services.events import log_event
from app.services.oss import (
    build_oss_key,
    delete_object,
    head_object,
    sign_get_url,
    sign_put_url,
    thumb_key_of,
)
from app.workers.tasks import enqueue_process_photo

router = APIRouter()


# ------------------------------------------------------------------------
# 上传闭环
# ------------------------------------------------------------------------
@router.post(
    "/upload-url",
    response_model=UploadUrlResponse,
    summary="申请 OSS 直传签名 URL",
)
async def get_upload_url(
    payload: UploadUrlRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UploadUrlResponse:
    """
    工作流：
    1. 若同用户同 hash 的照片已存在，直接返回 duplicate=true，客户端可跳过 PUT；
    2. 否则生成 OSS 直传签名 URL（PUT 方法，Content-Type 已绑定），
       并把客户端必须回带的 header 也一并告诉它。
    """
    # 1. 去重
    existing = await db.execute(
        select(Photo).where(
            Photo.user_id == current_user.id,
            Photo.hash == payload.hash,
        )
    )
    if existing.scalar_one_or_none():
        return UploadUrlResponse(
            upload_url="",
            oss_key="",
            headers={},
            expires_in=0,
            duplicate=True,
        )

    # 2. 签名
    try:
        oss_key = build_oss_key(str(current_user.id), payload.hash, payload.mime_type)
        signed = sign_put_url(oss_key, mime_type=payload.mime_type)
    except ValueError as exc:
        # mime 不支持
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UploadUrlResponse(
        upload_url=signed.url,
        oss_key=oss_key,
        headers=signed.headers,
        expires_in=signed.expires_in,
    )


@router.post(
    "",
    response_model=PhotoOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传完成回调（后端会去 OSS 核验对象真的存在）",
)
async def create_photo(
    payload: PhotoCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Photo:
    # 1) 二次去重（防并发）
    existing = await db.execute(
        select(Photo).where(
            Photo.user_id == current_user.id,
            Photo.hash == payload.hash,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Photo already exists",
        )

    # 2) key 路径必须落在当前用户的目录下，防止越权覆盖别人的对象
    expected_prefix = f"photos/{current_user.id}/"
    if not payload.oss_key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="oss_key does not belong to current user",
        )

    # 3) 到 OSS 上核验对象真的存在，且大小与请求一致
    try:
        meta = await head_object(payload.oss_key)
    except ServiceDegradedError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OSS service temporarily unavailable. Please retry later.",
        )
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Object not found on OSS. Please PUT first.",
        )
    if meta.size != payload.size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"OSS object size {meta.size} does not match request "
                f"size {payload.size_bytes}"
            ),
        )

    # 4) 落库；status = pending 等 worker 消费
    photo = Photo(
        user_id=current_user.id,
        oss_key=payload.oss_key,
        hash=payload.hash,
        size_bytes=payload.size_bytes,
        mime_type=payload.mime_type,
        status="pending",
    )
    db.add(photo)
    try:
        await db.commit()
    except IntegrityError:
        # 并发去重兜底：二次检查和 commit 之间另一请求已插入相同 (user_id, hash)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Photo already exists",
        )
    await db.refresh(photo)

    # 5) 入队 AI 处理任务
    await enqueue_process_photo(photo.id)

    return photo


# ------------------------------------------------------------------------
# 读取
# ------------------------------------------------------------------------
@router.get(
    "",
    response_model=list[PhotoListItem],
    summary="时间线：分页拉取当前用户照片",
)
async def list_photos(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[PhotoListItem]:
    stmt = (
        select(Photo)
        .where(Photo.user_id == current_user.id)
        .order_by(desc(func.coalesce(Photo.taken_at, Photo.created_at)))
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    photos = result.scalars().all()

    return [
        PhotoListItem(
            id=p.id,
            thumb_url=sign_get_url(p.thumb_key or p.oss_key),
            taken_at=p.taken_at,
            ai_description=p.ai_description,
            status=p.status,
        )
        for p in photos
    ]


@router.get(
    "/{photo_id}",
    response_model=PhotoOut,
    summary="照片详情",
)
async def get_photo(
    photo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Photo:
    result = await db.execute(
        select(Photo).where(
            Photo.id == photo_id,
            Photo.user_id == current_user.id,
        )
    )
    photo = result.scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return photo


@router.post(
    "/{photo_id}/interact",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="上报用户与单张照片的交互行为",
)
async def report_photo_interact(
    photo_id: str,
    payload: PhotoInteract,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """前端在查看/收藏/分享/下载照片时调用，写入 photo_interact 事件。"""
    photo = (
        await db.execute(
            select(Photo).where(
                Photo.id == photo_id,
                Photo.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    await log_event(
        user_id=current_user.id,
        event_type="photo_interact",
        payload={
            "photo_id": str(photo_id),
            "action": payload.action,
            "context": payload.context,
        },
    )


@router.delete(
    "/{photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除照片",
)
async def delete_photo(
    photo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    result = await db.execute(
        select(Photo).where(
            Photo.id == photo_id,
            Photo.user_id == current_user.id,
        )
    )
    photo = result.scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    # 删除 OSS 上的原图和缩略图（尽力而为，不阻断 DB 删除）
    oss_keys_to_delete = [photo.oss_key]
    if photo.thumb_key:
        oss_keys_to_delete.append(photo.thumb_key)
    else:
        thumb_key = thumb_key_of(photo.oss_key)
        oss_keys_to_delete.append(thumb_key)

    await db.delete(photo)
    await db.commit()

    for key in oss_keys_to_delete:
        try:
            await delete_object(key)
        except Exception:  # noqa: BLE001
            # OSS 删除失败不阻断流程，对象会被 OSS 生命周期规则清理
            pass
