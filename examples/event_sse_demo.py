"""
Photo Agent — Event Dict 结构示例 & SSE 对接代码

本文件包含三部分：
  1. 各事件类型的 dict 结构示例（带真实数据）
  2. 服务端 SSE 推送代码（FastAPI 端点）
  3. 客户端 SSE 消费代码（前端 EventSource）
"""

# ================================================================
# 1. Event Dict 结构示例
# ================================================================
# 每个 event 都是一个 dict，统一格式：
#   {
#     "type": str,          # 事件类型
#     "payload": dict,       # 具体内容（每种 type 不同）
#     "step": int,           # 当前循环步数
#     "timestamp": str,      # ISO 8601 时间戳
#     "elapsed_ms": int,     # 距任务开始的毫秒数
#   }
#
# 事件类型共 8 种：start / think / tool_call / tool_result / clarify / final / error / done

EVENT_EXAMPLES = {
    # ---- start: 任务开始 ----
    "start": {
        "type": "start",
        "payload": {
            "query": "帮我找去年夏天在海边拍的猫的照片",
            "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        },
        "step": 0,
        "timestamp": "2026-08-09T14:30:00.123456+00:00",
        "elapsed_ms": 0,
    },

    # ---- think: LLM 思考过程 ----
    # P1-3 修复后包含 tokens_used / total_tokens 用于可观测性
    "think": {
        "type": "think",
        "payload": {
            "reasoning": "用户想找海边猫的照片，需要搜索。先提取关键词：海边、猫、去年夏天。",
            "tokens_used": 342,
            "total_tokens": 342,
        },
        "step": 1,
        "timestamp": "2026-08-09T14:30:01.234567+00:00",
        "elapsed_ms": 1111,
    },

    # ---- tool_call: 工具调用 ----
    "tool_call": {
        "type": "tool_call",
        "payload": {
            "tool": "search_photos",
            "arguments": '{"query": "去年夏天在海边拍的猫", "from_date": "2025-06-01", "to_date": "2025-08-31"}',
        },
        "step": 1,
        "timestamp": "2026-08-09T14:30:01.345678+00:00",
        "elapsed_ms": 1222,
    },

    # ---- tool_result: 工具返回结果 ----
    # 注意：如果 result 的 JSON > 1500 bytes，会截断并追加标记
    "tool_result": {
        "type": "tool_result",
        "payload": {
            "tool": "search_photos",
            "result": {
                "ok": True,
                "items": [
                    {
                        "id": "f1e2d3c4-b5a6-7890-abcd-ef1234567890",
                        "thumb_url": "https://oss.example.com/photo/thumb_xxx?Expires=...",
                        "taken_at": "2025-07-15T10:30:00+08:00",
                        "ai_description": "一只橘猫趴在海边礁石上",
                        "status": "done",
                        "ai_analysis": {
                            "scene": "beach",
                            "objects": ["cat", "rock", "ocean"],
                            "mood": "relaxed",
                            "colors": ["orange", "blue", "gray"],
                        },
                        "score_semantic": 0.8923,
                        "score_recency": 0.7531,
                        "score_interaction": 0.6120,
                        "score_final": 0.8214,
                    },
                ],
                "parsed": {
                    "semantic": "海边 猫",
                    "from_date": "2025-06-01",
                    "to_date": "2025-08-31",
                    "tags": [],
                    "place": "海边",
                },
                "next_cursor": "eyJzIjowLjgyMTQsImlkIjoiZjFlMmQzYzQifQ==",
                "total": 1,
                "hint": "找到 1 张相关照片",
            },
        },
        "step": 1,
        "timestamp": "2026-08-09T14:30:02.456789+00:00",
        "elapsed_ms": 2333,
    },

    # ---- think: 第二步思考（LLM 看到搜索结果后决策）----
    "think_step2": {
        "type": "think",
        "payload": {
            "reasoning": "搜索到 1 张海边猫的照片，用户可能想对它进行改造。先推荐相关 Skill。",
            "tokens_used": 256,
            "total_tokens": 598,   # 342 + 256 累加
        },
        "step": 2,
        "timestamp": "2026-08-09T14:30:02.567890+00:00",
        "elapsed_ms": 2444,
    },

    # ---- tool_call: 推荐技能 ----
    "tool_call_recommend": {
        "type": "tool_call",
        "payload": {
            "tool": "recommend_skills",
            "arguments": '{"photo_ids": ["f1e2d3c4-b5a6-7890-abcd-ef1234567890"], "limit": 3}',
        },
        "step": 2,
        "timestamp": "2026-08-09T14:30:02.678901+00:00",
        "elapsed_ms": 2555,
    },

    # ---- tool_result: 推荐结果 ----
    "tool_result_recommend": {
        "type": "tool_result",
        "payload": {
            "tool": "recommend_skills",
            "result": {
                "ok": True,
                "items": [
                    {"skill_id": "s1...", "name": "动漫风", "model": "wanx2.1-imageedit"},
                    {"skill_id": "s2...", "name": "老照片修复", "model": "wanx2.1-imageedit"},
                    {"skill_id": "s3...", "name": "油画风格", "model": "wanx2.1-imageedit"},
                ],
                "hint": "为你推荐 3 个 Skill",
            },
        },
        "step": 2,
        "timestamp": "2026-08-09T14:30:02.789012+00:00",
        "elapsed_ms": 2666,
    },

    # ---- clarify: 需要用户澄清（暂停等待用户回复）----
    # 搜索失败 2 次后自动生成澄清选项
    "clarify": {
        "type": "clarify",
        "payload": {
            "question": "抱歉没找到完全匹配的照片，能帮我缩小一下范围吗？",
            "options": [
                "你想找哪个地点的照片？",
                "大概是哪段时间的照片？",
                "是人物照、风景照还是其他类型？",
                "先列出最近的照片让我自己挑",
            ],
        },
        "step": 3,
        "timestamp": "2026-08-09T14:30:05.123456+00:00",
        "elapsed_ms": 5000,
    },

    # ---- final: 最终回复 ----
    # reason 字段标识终止原因：normal / time_budget / token_budget
    "final": {
        "type": "final",
        "payload": {
            "message": "找到了这张海边橘猫的照片！如果你想对它进行 AI 改造（比如转动漫风、油画风），告诉我即可。",
        },
        "step": 3,
        "timestamp": "2026-08-09T14:30:03.890123+00:00",
        "elapsed_ms": 3767,
    },

    # ---- final: 预算超限终止 ----
    "final_budget": {
        "type": "final",
        "payload": {
            "message": "处理时间已超过上限（60s），请告诉我更具体的需求。",
            "reason": "time_budget",   # 或 "token_budget"
        },
        "step": 5,
        "timestamp": "2026-08-09T14:31:00.000000+00:00",
        "elapsed_ms": 60000,
    },

    # ---- error: 异常终止 ----
    "error": {
        "type": "error",
        "payload": {
            "message": "决策服务暂时不可用：ServiceDegradedError: dashscope_chat is degraded",
        },
        "step": 2,
        "timestamp": "2026-08-09T14:30:10.000000+00:00",
        "elapsed_ms": 10000,
    },

    # ---- done: 流式模式专用，标记会话结束 ----
    # 只在 POST /stream 的 SSE 流中出现，不在批量模式中
    "done": {
        "type": "done",
        "payload": {
            "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "status": "completed",   # completed / failed / active
            "state": {
                # AgentState.to_json() 的完整内容
                "step": 3,
                "search_attempts": 1,
                "clarification_attempts": 0,
                "confirmed_photo_id": None,
                "confirmed_generation_id": None,
                "last_search_items": [...],
                "fallback_level": 0,
                "total_tokens": 598,
                "total_cost": 0.0,
                "history": [...],
            },
        },
        "step": 3,
        "timestamp": "2026-08-09T14:30:03.890123+00:00",
        "elapsed_ms": 3767,
    },
}


# ================================================================
# 2. 服务端 SSE 推送代码（FastAPI）
# ================================================================
# 文件：app/api/agent.py
# 核心逻辑：用 asyncio.Queue 作为 Agent 和 SSE 之间的桥梁

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.services.agent import AgentState, PhotoAgent
from app.services.lock import AgentLock
from app.services.session import load_session, save_session

router = APIRouter()


@router.post("/stream", summary="流式运行 Photo Agent（SSE 实时推送）")
async def agent_stream(
    payload: AgentRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """与 /run 相同语义，但事件通过 Server-Sent Events 实时推送。

    前端用 EventSource 接收，事件格式（每行一个 SSE 数据帧）：
        data: {"type": "start", "payload": {...}, "step": 0}\\n\\n

    事件流顺序：
        start → think → tool_call → tool_result → ... → final/done
    """

    # ① 创建事件队列，maxsize=256 防止事件堆积导致内存溢出
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    # ② 后台 runner：运行 Agent，事件实时推入队列
    async def runner() -> None:
        try:
            # 获取分布式锁 → 运行 Agent → 保存会话
            state, _events, final_status = await _run_agent_with_lock(
                db, current_user, payload, use_stream=True, queue=queue
            )
            # Agent 循环结束后，推入 done 事件标记会话结束
            await queue.put({
                "type": "done",
                "payload": {
                    "session_id": str(state.session_id),
                    "status": final_status,
                    "state": state.to_json(),
                },
                "step": state.step,
            })
        except HTTPException as exc:
            # HTTP 异常（如 409 锁冲突、404 会话不存在）→ 推入 error 事件
            await queue.put({
                "type": "error",
                "payload": {
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
            })
        except Exception as exc:
            # 其他异常 → 推入 error 事件，截断过长的错误信息
            await queue.put({
                "type": "error",
                "payload": {"error": str(exc)[:500]},
            })

    # ③ 启动后台任务
    task = asyncio.create_task(runner())

    # ④ SSE 事件生成器：从队列取事件，格式化为 SSE 数据帧
    async def event_generator():
        while True:
            event = await queue.get()
            # SSE 协议格式：data: <json>\n\n
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            # 遇到 done 或 error 时终止流
            if event["type"] in ("done", "error"):
                break
        # 确保 runner task 完成（避免悬挂协程）
        if not task.done():
            await task

    # ⑤ 返回 StreamingResponse，设置 SSE 必需的 HTTP 头
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",        # 禁用缓存，确保实时性
            "Connection": "keep-alive",         # 保持长连接
            "X-Accel-Buffering": "no",          # Nginx 禁用缓冲，确保即时推送
        },
    )


# ================================================================
# 3. Agent 内部 emit() 函数 — 事件生产者
# ================================================================
# 文件：app/services/agent.py 中的 PhotoAgent.run()
# 这就是"每步产出 event dict"的核心实现

async def run_example(self, user_id, query, event_queue=None):
    """简化版 run()，只展示 emit 逻辑"""

    events: list[dict] = []              # 列表：批量模式用
    start_monotonic = time.monotonic()

    def emit(event_type: str, payload: dict) -> None:
        """生产一个事件 dict，同时写入列表和队列。"""
        elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
        event = {
            "type": event_type,
            "payload": payload,
            "step": state.step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
        }
        # ① 追加到列表（批量模式 /run 用）
        events.append(event)
        # ② 推入队列（流式模式 /stream 用）
        if event_queue is not None:
            try:
                event_queue.put_nowait(event)
            except asyncio.QueueFull:
                # 队列满了就丢弃事件，不阻塞 Agent 主循环
                logger.warning("Agent event queue is full, dropping event")

    # ---- 循环中各处调用 emit ----

    emit("start", {"query": query, "session_id": str(state.session_id)})

    while state.step < self.constraints.max_steps:
        state.step += 1

        # ... 预算检查 ...

        decision, usage = await _llm_decide(messages, tools)

        # emit think 事件
        emit("think", {
            "reasoning": decision.get("content", ""),
            "tokens_used": usage.get("total_tokens", 0),
            "total_tokens": state.total_tokens,
        })

        # 遍历 tool_calls
        for tc in tool_calls:
            tool_name = tc["function"]["name"]

            # emit tool_call 事件
            emit("tool_call", {"tool": tool_name, "arguments": arguments_str})

            result = await self._execute_tool(...)

            # emit tool_result 事件
            emit("tool_result", {"tool": tool_name, "result": result})

        # 终止
        emit("final", {"message": final_message})
        break

    return state, events


# ================================================================
# 4. 客户端 SSE 消费代码（前端 JavaScript）
# ================================================================
# 前端用原生 EventSource API 接收事件流

CLIENT_JS = """
// ===== 前端 EventSource 消费示例 =====

const sessionId = null; // 新会话；续接时传入已有 session_id

// 构造请求
const response = await fetch('/api/agent/stream', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
  },
  body: JSON.stringify({
    query: '帮我找去年夏天在海边拍的猫的照片',
    session_id: sessionId,
  }),
});

// 用 ReadableStream 读取 SSE（因为 fetch + EventSource 不支持 POST + Headers）
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });

  // SSE 以 \\n\\n 分隔每条消息
  const messages = buffer.split('\\n\\n');
  buffer = messages.pop(); // 最后一个可能不完整，留着下次

  for (const msg of messages) {
    if (!msg.startsWith('data: ')) continue;

    const jsonStr = msg.slice(6); // 去掉 "data: " 前缀
    const event = JSON.parse(jsonStr);

    // 根据 type 分发处理
    switch (event.type) {
      case 'start':
        console.log('任务开始', event.payload.session_id);
        showLoading();
        break;

      case 'think':
        // 展示 LLM 思考过程（带 Token 用量）
        console.log(`[Step ${event.step}] 思考: ${event.payload.reasoning}`);
        console.log(`  Token: ${event.payload.tokens_used} (累计 ${event.payload.total_tokens})`);
        showThinking(event.payload.reasoning);
        break;

      case 'tool_call':
        console.log(`[Step ${event.step}] 调用工具: ${event.payload.tool}`);
        showToolCalling(event.payload.tool, event.payload.arguments);
        break;

      case 'tool_result':
        // 搜索结果等数据
        const result = event.payload.result;
        if (result.ok && result.items) {
          renderPhotos(result.items);
        }
        break;

      case 'clarify':
        // 需要用户澄清，展示选项按钮
        showClarifyOptions(event.payload.question, event.payload.options);
        break;

      case 'final':
        // 最终回复
        console.log(`耗时 ${event.elapsed_ms}ms`);
        showFinalMessage(event.payload.message);
        hideLoading();
        break;

      case 'error':
        console.error('错误:', event.payload);
        showError(event.payload.message || event.payload.error);
        hideLoading();
        break;

      case 'done':
        // 会话结束，保存 session_id 用于续接
        const sessionId = event.payload.session_id;
        const status = event.payload.status; // completed / failed / active
        console.log(`会话结束: ${status}, session_id: ${sessionId}`);
        saveSessionId(sessionId);
        break;
    }
  }
}
"""

# ================================================================
# 5. 完整事件流时序图
# ================================================================
# 一次完整的搜索 + 推荐 + 最终回复的事件流：
#
#   客户端                           服务端
#     |                                |
#     |  POST /stream {query}          |
#     |------------------------------->|
#     |                                |  获取锁 → 运行 Agent
#     |  data: {"type":"start",...}    |
#     |<-------------------------------|  emit("start")
#     |                                |
#     |                                |  调用 LLM (step 1)
#     |  data: {"type":"think",...}    |
#     |<-------------------------------|  emit("think")
#     |                                |
#     |  data: {"type":"tool_call",..} |
#     |<-------------------------------|  emit("tool_call")
#     |                                |  执行 search_photos()
#     |  data: {"type":"tool_result"..}|
#     |<-------------------------------|  emit("tool_result")
#     |                                |
#     |                                |  调用 LLM (step 2)
#     |  data: {"type":"think",...}    |
#     |<-------------------------------|  emit("think")
#     |                                |
#     |  data: {"type":"tool_call",..} |
#     |<-------------------------------|  emit("tool_call")
#     |                                |  执行 recommend_skills()
#     |  data: {"type":"tool_result"..}|
#     |<-------------------------------|  emit("tool_result")
#     |                                |
#     |                                |  调用 LLM (step 3) → final_answer
#     |  data: {"type":"final",...}    |
#     |<-------------------------------|  emit("final")
#     |                                |
#     |  data: {"type":"done",...}     |
#     |<-------------------------------|  runner 推入 done
#     |    (流关闭)                     |  释放锁 → 保存会话
#     |                                |


if __name__ == "__main__":
    # 打印所有事件类型的结构示例
    import pprint

    for name, event in EVENT_EXAMPLES.items():
        print(f"\n{'='*60}")
        print(f"事件类型: {event['type']}" + (f" (示例名: {name})" if name != event['type'] else ""))
        print(f"{'='*60}")
        pprint.pprint(event, width=100, sort_dicts=False)
