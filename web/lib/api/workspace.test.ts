import { afterEach, expect, it, vi } from 'vitest';
import { apiClient } from './client';
import { getWorkspace, updateWorkspace, undoWorkspace } from './workspace';
afterEach(() => vi.restoreAllMocks());
it('does not treat legacy HTTP 200 auth envelopes as successful workspace operations', async () => {
  const data = { errNo: 10002, errMsg: 'Missing Authorization header', data: null };
  vi.spyOn(apiClient, 'GET').mockResolvedValue({ data, response: new Response() } as never);
  vi.spyOn(apiClient, 'POST').mockResolvedValue({ data, response: new Response() } as never);
  await expect(getWorkspace()).rejects.toThrow('Missing Authorization');
  await expect(updateWorkspace({ kind: 'clear_selection' }, 1, 'operation-key')).rejects.toThrow('Missing Authorization');
  await expect(undoWorkspace('op', 1)).rejects.toThrow('Missing Authorization');
});
