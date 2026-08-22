import { describe, expect, it } from 'vitest';
import { toApiFailure } from './client';

describe('API error normalization', () => {
  it('keeps backend detail and trace headers', async () => {
    const response = new Response(null, {
      status: 422,
      headers: {
        'X-Log-ID': 'web-log-1',
        'X-Trace-ID': 'trace-1',
      },
    });

    const failure = await toApiFailure(response, { detail: '参数不合法' });

    expect(failure.status).toBe(422);
    expect(failure.detail).toBe('参数不合法');
    expect(failure.logId).toBe('web-log-1');
    expect(failure.traceId).toBe('trace-1');
  });

  it('extracts generation domain messages from nested details', async () => {
    const response = new Response(null, { status: 429 });
    const failure = await toApiFailure(response, {
      detail: { code: 'quota_exceeded', message: '今日生成额度已用完' },
    });

    expect(failure.detail).toBe('今日生成额度已用完');
  });
});
