# Agent 事件与 SSE 接入示例

这份示例描述 `PhotoAgent.run()` 产生的事件以及前端消费 SSE 的方式。它是协议说明，
不是可直接执行的 Python 模块；真实服务入口位于 `app/api/agent.py`。

## 事件顺序

一次典型请求会依次产生：

```text
start -> think -> tool_call -> tool_result -> ... -> final
                                            \-> clarify
                                            \-> error
```

每个事件都包含：

```json
{
  "type": "tool_call",
  "payload": {
    "tool": "search_photos",
    "arguments": "{\"query\":\"海边的照片\"}"
  },
  "step": 1,
  "timestamp": "2026-08-11T00:00:00+00:00",
  "elapsed_ms": 35
}
```

## HTTP 调用

先完成登录并取得 JWT，然后请求流式接口：

```bash
curl -N -X POST http://localhost:8000/agent/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"帮我找海边拍的照片"}'
```

`-N` 用于关闭 curl 的输出缓冲，便于实时查看事件。

## 浏览器端消费

接口使用 POST，不能直接用原生 `EventSource`。可通过 `fetch` 读取响应流：

```javascript
const response = await fetch(`${baseUrl}/agent/stream`, {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ query: '帮我找海边拍的照片' })
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const blocks = buffer.split('\n\n');
  buffer = blocks.pop() || '';

  for (const block of blocks) {
    const dataLine = block.split('\n').find(line => line.startsWith('data: '));
    if (!dataLine) continue;
    const event = JSON.parse(dataLine.slice(6));
    console.log(event.type, event.payload);
  }
}
```

## 终态处理

- `final`：任务结束，展示 `payload.message`。
- `clarify`：暂停当前任务，展示问题和候选项；用户回复时携带原 `session_id`。
- `error`：展示可重试提示并记录响应头 `X-Log-ID`，便于后端排查。

## 注意事项

- 客户端应处理事件被拆分到多个网络数据块的情况。
- 不要把 `think.reasoning` 当作稳定业务协议；稳定字段以 schema 和事件类型为准。
- 服务端队列有容量限制，客户端处理过慢时应允许丢弃中间进度事件，但不能丢终态。
