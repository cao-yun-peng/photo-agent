import createClient, { type Middleware } from 'openapi-fetch';
import { clearSession, readSession } from '@/lib/auth/session';
import type { paths } from './generated';

export const API_ORIGIN = (
  process.env.NEXT_PUBLIC_API_ORIGIN || 'http://localhost:8000'
).replace(/\/$/, '');

export function newRequestLogId(): string {
  const random = crypto.randomUUID?.() || Math.random().toString(36).slice(2);
  return `web-${Date.now().toString(36)}-${random.slice(0, 8)}`;
}

const requestMiddleware: Middleware = {
  async onRequest({ request }) {
    const session = readSession();
    request.headers.set('X-Log-ID', newRequestLogId());
    if (session) {
      request.headers.set('Authorization', `Bearer ${session.accessToken}`);
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) clearSession();
    return response;
  },
};

export const apiClient = createClient<paths>({ baseUrl: API_ORIGIN });
apiClient.use(requestMiddleware);

export interface ApiFailure extends Error {
  status: number;
  detail: string;
  logId: string | null;
  traceId: string | null;
}

export async function toApiFailure(
  response: Response,
  errorBody: unknown,
): Promise<ApiFailure> {
  const body = errorBody as
    | { detail?: unknown; message?: unknown; errMsg?: unknown }
    | undefined;
  const detailValue = body?.detail ?? body?.message ?? body?.errMsg;
  const detail =
    typeof detailValue === 'string'
      ? detailValue
      : detailValue
        ? JSON.stringify(detailValue)
        : `请求失败（HTTP ${response.status}）`;
  return Object.assign(new Error(detail), {
    status: response.status,
    detail,
    logId: response.headers.get('X-Log-ID'),
    traceId: response.headers.get('X-Trace-ID'),
  });
}
