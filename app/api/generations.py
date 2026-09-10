"""生成相关路由：准备、确认、查询生成任务。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status, Path, Response
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.generation import Generation, GenerationInput
from app.models.user import User
from app.schemas.skill import (
    GenerateRequest,
    GenerationIterationRequest,
    GenerationConfirmRequest,
    GenerationOut,
)
from app.services.generation_service import (
    GenerationDomainError,
    confirm_generation,
    prepare_generation,
)
from app.services.oss import sign_get_url
from app.services.rollout import agent_variant_for_user
from app.models.provider_operation import ProviderCall
from app.services.provider_ledger import summarize_calls
from app.schemas.provider_cost import ProviderCostOut

router = APIRouter()


@router.get(
    "/provider-calls",
    response_model=ProviderCostOut,
    summary="当前用户最近调用费用，包含未能创建方案的调用",
)
async def recent_provider_calls(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (
        (
            await db.execute(
                select(ProviderCall)
                .where(ProviderCall.user_id == current_user.id)
                .order_by(ProviderCall.created_at.desc(), ProviderCall.id.desc())
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    return {**summarize_calls(rows[:100]), "truncated": len(rows) > 100, "limit": 100}


@router.get(
    "/generations/{generation_id}/cost",
    response_model=ProviderCostOut,
    summary="规划、生图和核验的逐次用量与费用估算",
)
async def generation_call_cost(
    generation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from sqlalchemy import or_

    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == generation_id, Generation.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    if gen is None:
        raise HTTPException(404, "生成任务不存在")
    planning_id = (gen.execution_snapshot or {}).get("planning_operation_id")
    clauses = [ProviderCall.generation_id == gen.id]
    if planning_id:
        clauses.append(ProviderCall.planning_id == UUID(planning_id))
    rows = (
        (
            await db.execute(
                select(ProviderCall)
                .where(ProviderCall.user_id == current_user.id, or_(*clauses))
                .order_by(ProviderCall.created_at, ProviderCall.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        **summarize_calls(rows),
        "generation_id": str(gen.id),
        "scope_note": "仅本次方案和生成；不含父任务。共享规划按调用ID去重，不能直接累加不同任务摘要。",
    }


def _to_out(g: Generation) -> GenerationOut:
    return GenerationOut(
        id=g.id,
        progress_stage=g.progress_stage,
        parent_generation_id=g.parent_generation_id,
        root_generation_id=g.root_generation_id,
        iteration_index=g.iteration_index,
        execution_snapshot=g.execution_snapshot,
        execution_digest=g.execution_digest,
        verification=g.verification,
        source_photo_id=g.source_photo_id,
        skill_id=g.skill_id,
        extra_prompt=g.extra_prompt,
        result_oss_key=g.result_oss_key,
        result_url=sign_get_url(g.result_oss_key) if g.result_oss_key else None,
        status=g.status,
        error_message=g.error_message,
        model=g.model,
        cost_yuan=g.cost_yuan,
        estimated_cost_yuan=g.estimated_cost_yuan,
        confirmation_token=g.confirmation_token,
        confirmation_expires_at=g.confirmation_expires_at,
        enqueue_status=g.enqueue_status,
        attempt_count=g.attempt_count,
        created_at=g.created_at,
    )


def _raise_domain_error(exc: GenerationDomainError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.post(
    "/photos/{photo_id}/generate",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="用 Skill 对某张照片做 AI 改造（异步）",
)
async def create_generation(
    photo_id: UUID,
    payload: GenerateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    try:
        gen = await prepare_generation(
            db=db,
            user_id=current_user.id,
            photo_id=photo_id,
            skill_id=payload.skill_id,
            extra_prompt=payload.extra_prompt,
            model=payload.model,
            idempotency_key=payload.idempotency_key,
            package_options=payload.package_options,
        )
        # 控制组保留旧的一步式体验；v2 灰度组必须显式确认。
        if (
            not gen.execution_snapshot
            and agent_variant_for_user(current_user.id) == "control"
            and gen.status
            in {
                "awaiting_confirmation",
                "queue_failed",
            }
        ):
            gen = await confirm_generation(
                db=db,
                user_id=current_user.id,
                generation_id=gen.id,
                confirmation_token=gen.confirmation_token,
            )
    except GenerationDomainError as exc:
        _raise_domain_error(exc)
    return _to_out(gen)


@router.post(
    "/generations/{generation_id}/confirm",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="确认并入队生成任务（可幂等重试）",
)
async def confirm_generation_route(
    generation_id: UUID,
    payload: GenerationConfirmRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    try:
        generation = await confirm_generation(
            db=db,
            user_id=current_user.id,
            generation_id=generation_id,
            confirmation_token=payload.confirmation_token,
            execution_digest=payload.execution_digest,
        )
    except GenerationDomainError as exc:
        _raise_domain_error(exc)
    return _to_out(generation)


@router.get(
    "/generations",
    response_model=list[GenerationOut],
    summary="我的生成历史",
)
async def list_my_generations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[GenerationOut]:
    result = await db.execute(
        select(Generation)
        .where(Generation.user_id == current_user.id)
        .order_by(desc(Generation.created_at))
        .limit(limit)
        .offset(offset)
    )
    return [_to_out(g) for g in result.scalars().all()]


@router.get(
    "/generations/{generation_id}",
    response_model=GenerationOut,
    summary="生成任务详情（用于轮询状态）",
)
async def get_generation(
    generation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    g = (
        await db.execute(
            select(Generation).where(
                and_(
                    Generation.id == generation_id,
                    Generation.user_id == current_user.id,
                )
            )
        )
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return _to_out(g)


@router.get("/generations/{generation_id}/inputs/{position}", response_class=Response)
async def get_generation_input(
    generation_id: UUID,
    position: Annotated[int, Path(ge=0, le=4)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    item = (
        await db.execute(
            select(GenerationInput)
            .join(Generation)
            .where(
                Generation.id == generation_id,
                Generation.user_id == current_user.id,
                GenerationInput.position == position,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Frozen input not found")
    return Response(
        item.content,
        media_type=item.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/generations/{generation_id}/cancel", response_model=GenerationOut)
async def cancel_generation_route(
    generation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.generation_lifecycle import cancel_generation

    try:
        return _to_out(await cancel_generation(db, current_user.id, generation_id))
    except GenerationDomainError as exc:
        _raise_domain_error(exc)


@router.post(
    "/generations/{generation_id}/iterations",
    response_model=GenerationOut,
    status_code=202,
)
async def iterate_generation_route(
    generation_id: UUID,
    payload: GenerationIterationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.generation_lifecycle import prepare_iteration

    try:
        return _to_out(
            await prepare_iteration(
                db,
                current_user.id,
                generation_id,
                payload.feedback,
                payload.idempotency_key,
            )
        )
    except GenerationDomainError as exc:
        _raise_domain_error(exc)
