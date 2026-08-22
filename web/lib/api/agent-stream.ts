import type { components } from './generated';
import { clearSession, readSession } from '@/lib/auth/session';
import {
  API_ORIGIN,
  newRequestLogId,
  toApiFailure,
  type ApiFailure,
} from './client';

export type AgentRunRequest = components['schemas']['AgentRunRequest'];

export interface AgentEvent {
  type: string;
  payload: Record<string, unknown>;
  step?: number;
  timestamp?: string;
  elapsed_ms?: number;
}

export interface AgentStreamOptions {
  signal?: AbortSignal;
  onEvent?: (event: AgentEvent) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseEvent(data: string): AgentEvent {
  const value: unknown = JSON.parse(data);
  if (!isRecord(value) || typeof value.type !== 'string') {
    throw new Error('Agent 流包含无效事件');
  }

  return {
    type: value.type,
    payload: isRecord(value.payload) ? value.payload : {},
    ...(typeof value.step === 'number' ? { step: value.step } : {}),
    ...(typeof value.timestamp === 'string' ? { timestamp: value.timestamp } : {}),
    ...(typeof value.elapsed_ms === 'number' ? { elapsed_ms: value.elapsed_ms } : {}),
  };
}

function frameBoundary(buffer: string): { index: number; length: number } | null {
  const matches = [
    { index: buffer.indexOf('\r\n\r\n'), length: 4 },
    { index: buffer.indexOf('\n\n'), length: 2 },
    { index: buffer.indexOf('\r\r'), length: 2 },
  ].filter((match) => match.index >= 0);
  matches.sort((left, right) => left.index - right.index);
  return matches[0] || null;
}

export function createSseParser(onEvent: (event: AgentEvent) => void) {
  let buffer = '';

  const parseFrame = (frame: string) => {
    const data = frame
      .split(/\r\n|\r|\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, ''))
      .join('\n');
    if (data) onEvent(parseEvent(data));
  };

  return {
    feed(text: string) {
      buffer += text;
      let boundary = frameBoundary(buffer);
      while (boundary) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        if (frame.trim()) parseFrame(frame);
        boundary = frameBoundary(buffer);
      }
    },
    flush() {
      if (buffer.trim()) parseFrame(buffer);
      buffer = '';
    },
  };
}

export async function parseAgentEventStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser(onEvent);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }
  parser.feed(decoder.decode());
  parser.flush();
}

function eventFailure(payload: Record<string, unknown>): ApiFailure {
  const rawDetail = payload.detail ?? payload.message ?? payload.error;
  let detail = 'Agent 执行失败';
  if (typeof rawDetail === 'string') {
    detail = rawDetail;
  } else if (isRecord(rawDetail) && typeof rawDetail.message === 'string') {
    detail = rawDetail.message;
  } else if (rawDetail) {
    detail = JSON.stringify(rawDetail);
  }

  return Object.assign(new Error(detail), {
    status: typeof payload.status_code === 'number' ? payload.status_code : 0,
    detail,
    logId: null,
    traceId: null,
  });
}

export async function streamAgent(
  request: AgentRunRequest,
  options: AgentStreamOptions = {},
): Promise<AgentEvent[]> {
  const session = readSession();
  const response = await fetch(`${API_ORIGIN}/agent/stream`, {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      'X-Log-ID': newRequestLogId(),
      ...(session ? { Authorization: `Bearer ${session.accessToken}` } : {}),
    },
    body: JSON.stringify(request),
    signal: options.signal,
  });

  if (response.status === 401) clearSession();
  if (!response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = { detail: await response.text().catch(() => '') };
    }
    throw await toApiFailure(response, body);
  }
  if (!response.body) {
    throw Object.assign(new Error('浏览器未提供 Agent 响应流'), {
      status: 0,
      detail: '浏览器未提供 Agent 响应流',
      logId: response.headers.get('X-Log-ID'),
      traceId: response.headers.get('X-Trace-ID'),
    }) satisfies ApiFailure;
  }

  const events: AgentEvent[] = [];
  let streamError: ApiFailure | null = null;
  await parseAgentEventStream(response.body, (event) => {
    events.push(event);
    options.onEvent?.(event);
    if (event.type === 'error') streamError = eventFailure(event.payload);
  });
  if (streamError) throw streamError;
  return events;
}
