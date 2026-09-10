"""Agent 对话路由：多轮会话 + 主动澄清入口 + SSE 流式推送."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.security import get_current_user
from app.database import AsyncSessionLocal
from app.models.user import User
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.services.agent import AgentState, PhotoAgent
from app.services.agent_workflow import transition_workflow
from app.services.lock import AgentLock
from app.config import settings
from app.services.agent_ownership import claim_agent_run
from app.services.task_lifecycle import OwnershipLost, cancel_and_wait, run_cleanup
from uuid import UUID
from app.services.session import load_session, save_session

logger = logging.getLogger(__name__)

router = APIRouter()


async def _execute_agent(db, user_id, payload, queue):
    initial_state: AgentState | None = None
    if payload.session_id:
        session = await load_session(db, payload.session_id, user_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or expired",
            )
        initial_state = AgentState.from_json(session.state, session_id=session.id)

    if payload.selected_photo_id:
        if initial_state is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="选择照片时必须续接产生该候选列表的会话",
            )
        selected_id = str(payload.selected_photo_id)
        candidate_ids = {
            str(item.get("id"))
            for item in initial_state.last_search_items
            if isinstance(item, dict) and item.get("id")
        }
        candidate_ids.update(
            str(photo_id)
            for photo_id in initial_state.active_search.get("shown_photo_ids", [])
            if photo_id
        )
        if (
            selected_id not in candidate_ids
            or selected_id in initial_state.rejected_photo_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="所选照片不在当前候选列表中，请重新搜索后选择",
            )
        initial_state.feedback_undo = None
        initial_state.confirmed_photo_id = selected_id
        transition_workflow(initial_state, "selection_confirmed")

    if initial_state is not None:
        initial_state.feedback_batch_id = payload.feedback_batch_id

    agent = PhotoAgent(db=db)
    if getattr(payload, "ui_action", None):
        if initial_state is None or payload.selected_photo_id:
            raise HTTPException(400, detail="操作需要当前搜索会话")
        from app.services.agent_ui_actions import run_ui_action

        state, events = await run_ui_action(
            agent, user_id, payload, initial_state, queue
        )
    else:
        state, events = await agent.run(
            user_id=user_id,
            query=payload.query,
            initial_state=initial_state,
            event_queue=queue,
        )

    final_status = "active"
    if events:
        last_type = events[-1]["type"]
        if last_type == "final":
            final_status = "completed"
        elif last_type == "error":
            final_status = "failed"
        # "clarify" 保持 "active" — 等待用户回复

    await save_session(db, state, status=final_status)
    await db.commit()
    return state, events, final_status


async def _run_agent_with_lock(
    user_id: UUID,
    payload: AgentRunRequest,
    queue: asyncio.Queue | None = None,
) -> tuple[AgentState, list[dict], str]:
    lock = AgentLock(str(user_id))
    if not await lock.acquire():
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "agent_busy", "retry_after": 5}
        )
    renewal = execution = watcher = None
    try:
        renewal = await lock.start_auto_renew()

        async def execute():
            async with AsyncSessionLocal() as db:
                await claim_agent_run(db, user_id, lock)
                return await _execute_agent(db, user_id, payload, queue)

        execution = asyncio.create_task(execute(), name="agent-execution")

        async def watch_loss():
            await lock.lost.wait()
            execution.cancel()

        watcher = asyncio.create_task(watch_loss(), name="agent-ownership-watch")
        try:
            async with asyncio.timeout(settings.agent_max_time_seconds):
                result = await execution
                await lock.assert_owned()
                return result
        except (OwnershipLost, asyncio.CancelledError):
            if lock.lost.is_set():
                raise HTTPException(503, detail={"error": "agent_lock_lost"}) from None
            raise
        except TimeoutError:
            raise HTTPException(504, detail={"error": "agent_deadline"}) from None
    finally:
        lock.lost.set()
        await cancel_and_wait(execution, watcher, renewal)
        await run_cleanup(lock.release)


@router.post(
    "/run",
    response_model=AgentRunResponse,
    summary="运行 Photo Agent（支持新建/续接会话）",
)
async def agent_run(
    payload: AgentRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> AgentRunResponse:
    """
    用户输入一条自然语言请求，Agent 决定搜索、澄清或生成。

    - 不带 session_id：新建会话；
    - 带 session_id：续接之前会话，保留搜索历史、已澄清次数、兜底级别等状态。

    同一用户的并发请求会被 Redis 分布式锁串行化，避免状态混乱。
    """
    state, events, final_status = await _run_agent_with_lock(current_user.id, payload)

    return AgentRunResponse(
        session_id=state.session_id,
        events=events,
        state=state.to_json(),
        status=final_status,
    )


@router.post(
    "/stream",
    summary="流式运行 Photo Agent（SSE，实时推送思考/工具调用/结果）",
)
async def agent_stream(
    payload: AgentRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    """与 /run 相同语义，但事件通过 Server-Sent Events 实时推送。

    事件格式（每行一个 SSE 数据帧）：
      data: {"type": "start", "payload": {...}, "step": 0}\n\n
    前端可用 EventSource 接收并渲染：
      - start: 任务开始
      - route: 当前轮次的意图与上下文关系（new/replace/refine/continue）
      - think: 复杂请求进入 Agent 后的 LLM 思考过程
      - tool_call / tool_result: 工具调用与结果
      - clarify: 需要用户澄清
      - final: 最终回复
      - done: 会话结束，payload 包含 session_id 与最终 status

    同一用户的并发请求会被 Redis 分布式锁串行化。
    """
    # Only immutable identity crosses the request dependency lifetime.
    user_id = current_user.id
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    async def runner() -> None:
        try:
            state, _events, final_status = await _run_agent_with_lock(
                user_id, payload, queue
            )
            await queue.put(
                {
                    "type": "done",
                    "payload": {
                        "session_id": str(state.session_id),
                        "status": final_status,
                        "state": state.to_json(),
                    },
                    "step": state.step,
                }
            )
        except HTTPException as exc:
            await queue.put(
                {
                    "type": "error",
                    "payload": {"status_code": exc.status_code, "detail": exc.detail},
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent stream runner failed")
            await queue.put({"type": "error", "payload": {"error": "agent_failed"}})

    async def event_generator():
        # Start only when consumption starts; an unconsumed response owns no task.
        task = asyncio.create_task(runner(), name="agent-stream-runner")
        pending = None
        try:
            while True:
                if task.done() and queue.empty():
                    await task
                    break
                pending = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {pending, task}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending not in done:
                    if not queue.empty():
                        event = await pending
                    else:
                        await task
                        break
                else:
                    event = pending.result()
                pending = None
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in {"done", "error"}:
                    break
        finally:
            await cancel_and_wait(pending, task)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
