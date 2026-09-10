import type { components } from './generated';
import { clearSessionIfCurrent, readSession, sessionEpoch, sessionSignal } from '@/lib/auth/session';
import {
  API_ORIGIN,
  newRequestLogId,
  toApiFailure,
  type ApiFailure,
} from './client';

export type AgentUIAction = { action: 'reject_photo'; photo_id: string; batch_id: string } | { action: 'undo_feedback'; undo_id: string } | { action: 'continue_search' };
export type AgentRunRequest = components['schemas']['AgentRunRequest'] & { ui_action?: AgentUIAction };

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

export function createSseParser(onEvent: (event: AgentEvent) => void) {
  const onData = (data: string) => { if (data) onEvent(parseEvent(data)); };
  let line = '';
  let data: string[] = [];
  let skipLf = false;
  function finishLine() {
    if (line === '') {
      if (data.length) onData(data.join('\n'));
      data = [];
    } else if (line === 'data' || line.startsWith('data:')) {
      data.push(line === 'data' ? '' : line.slice(5).replace(/^ /, ''));
    }
    line = '';
  }
  return {
    feed(text: string) {
      for (const char of text) {
        if (skipLf) {
          skipLf = false;
          if (char === '\n') continue;
        }
        if (char === '\r' || char === '\n') {
          finishLine();
          skipLf = char === '\r';
        } else line += char;
      }
    },
    flush() {
      if (line) finishLine();
      if (data.length) onData(data.join('\n'));
      data = [];
      skipLf = false;
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
  const epoch = sessionEpoch();
  const response = await fetch(`${API_ORIGIN}/agent/stream`, {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'X-Timezone': Intl.DateTimeFormat().resolvedOptions().timeZone,
      'Content-Type': 'application/json',
      'X-Log-ID': newRequestLogId(),
      ...(session ? { Authorization: `Bearer ${session.accessToken}` } : {}),
    },
    body: JSON.stringify(request),
    signal: options.signal ? AbortSignal.any([options.signal, sessionSignal()]) : sessionSignal(),
  });

  if (response.status === 401) clearSessionIfCurrent(session?.accessToken, epoch);
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
    if (epoch !== sessionEpoch()) throw new DOMException('Session changed', 'AbortError');
    events.push(event);
    options.onEvent?.(event);
    if (event.type === 'error') streamError = eventFailure(event.payload);
  });
  if (streamError) throw streamError;
  return events;
}
