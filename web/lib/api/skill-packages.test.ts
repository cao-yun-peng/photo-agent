import { afterEach, expect, it, vi } from 'vitest';
import { apiClient } from './client';
import { importSkillPackage, listSkillVersions, previewSkillPackage, readSkillAsset } from './skills';

afterEach(() => vi.restoreAllMocks());

it('rejects HTTP 200 authentication error envelopes instead of treating them as a package', async () => {
  const envelope = { errNo: 10002, errMsg: 'Missing Authorization header', data: null };
  vi.spyOn(apiClient, 'POST').mockResolvedValue({ data: envelope, response: new Response() } as never);
  vi.spyOn(apiClient, 'GET').mockResolvedValueOnce({ data: envelope, response: new Response() } as never)
    .mockResolvedValueOnce({ data: new Blob([JSON.stringify(envelope)], { type: 'application/json' }), response: new Response() } as never);
  const file = new File(['zip'], 'skill.zip');
  await expect(previewSkillPackage(file)).rejects.toThrow('Missing Authorization');
  await expect(importSkillPackage(file, 'a'.repeat(64))).rejects.toThrow('Missing Authorization');
  await expect(listSkillVersions('skill')).rejects.toThrow('Missing Authorization');
  await expect(readSkillAsset('skill', 'version', 'SKILL.md')).rejects.toThrow('Missing Authorization');
});
