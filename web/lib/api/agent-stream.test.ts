import { describe, expect, it, vi } from 'vitest';
import {
  createSseParser,
  parseAgentEventStream,
  streamAgent,
  type AgentEvent,
} from './agent-stream';

function byteStream(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    },
  });
}

describe('Agent SSE parser', () => {
  it('parses frames split at arbitrary text boundaries', () => {
    const events: AgentEvent[] = [];
    const parser = createSseParser((event) => events.push(event));

    parser.feed('data: {"type":"sta');
    parser.feed('rt","payload":{"query":"猫"}}\r\n\r');
    parser.feed('\ndata: {"type":"done","payload":{}}\n\n');
    parser.flush();

    expect(events.map((event) => event.type)).toEqual(['start', 'done']);
    expect(events[0].payload.query).toBe('猫');
  });

  it('combines multiple data lines and ignores SSE comments', () => {
    const events: AgentEvent[] = [];
    const parser = createSseParser((event) => events.push(event));

    parser.feed(': heartbeat\n');
    parser.feed('data: {"type":"clarify",\n');
    parser.feed('data: "payload":{"question":"哪一天？"}}\n\n');

    expect(events[0]).toMatchObject({
      type: 'clarify',
      payload: { question: '哪一天？' },
    });
  });

  it('decodes a Chinese UTF-8 character split across byte chunks', async () => {
    const encoded = new TextEncoder().encode(
      'data: {"type":"final","payload":{"message":"找到猫咪了"}}\n\n',
    );
    const splitAt = encoded.indexOf(0xe7) + 1;
    const events: AgentEvent[] = [];

    await parseAgentEventStream(
      byteStream([encoded.slice(0, splitAt), encoded.slice(splitAt)]),
      (event) => events.push(event),
    );

    expect(events[0].payload.message).toBe('找到猫咪了');
  });

  it('surfaces an SSE error event as an API failure', async () => {
    const encoded = new TextEncoder().encode(
      'data: {"type":"error","payload":{"status_code":409,"detail":{"message":"正在处理上一个请求"}}}\n\n',
    );
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(byteStream([encoded]), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    })));

    await expect(streamAgent({ query: '继续找' })).rejects.toMatchObject({
      status: 409,
      detail: '正在处理上一个请求',
    });
    vi.unstubAllGlobals();
  });
});
