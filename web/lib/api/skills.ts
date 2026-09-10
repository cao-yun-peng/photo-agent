import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type Skill = components['schemas']['SkillOut'];
export type SkillCreate = components['schemas']['SkillCreate'];
export type SkillUpdate = components['schemas']['SkillUpdate'];
export type QuotaInfo = components['schemas']['QuotaInfo'];
export type PackageReport = components['schemas']['PackageReport'];
export type PackageVersion = components['schemas']['PackageVersionOut'];

export async function previewSkillPackage(file: File): Promise<PackageReport> {
  const { data, error, response } = await apiClient.POST('/skills/packages/preview', {
    body: file as unknown as string,
    bodySerializer: () => file,
    headers: { 'Content-Type': 'application/zip' },
  });
  if (!data || typeof data.content_sha256 !== 'string') throw await toApiFailure(response, error || data);
  return data;
}

export async function importSkillPackage(file: File, hash: string, skillId?: string) {
  const { data, error, response } = await apiClient.POST('/skills/packages/import', {
    params: { query: { expected_hash: hash, skill_id: skillId } },
    body: file as unknown as string,
    bodySerializer: () => file,
    headers: { 'Content-Type': 'application/zip' },
  });
  if (!data || typeof data.version_id !== 'string') throw await toApiFailure(response, error || data);
  return data;
}

export async function listSkillVersions(skillId: string): Promise<PackageVersion[]> {
  const { data, error, response } = await apiClient.GET('/skills/{skill_id}/versions', {
    params: { path: { skill_id: skillId } },
  });
  if (!Array.isArray(data)) throw await toApiFailure(response, error || data);
  return data;
}

export async function readSkillAsset(skillId: string, versionId: string, path: string): Promise<Blob> {
  const { data, error, response } = await apiClient.GET('/skills/{skill_id}/versions/{version_id}/assets', {
    params: { path: { skill_id: skillId, version_id: versionId }, query: { path } },
    parseAs: 'blob',
  });
  if (!data) throw await toApiFailure(response, error);
  if (data.type.includes('application/json')) throw await toApiFailure(response, JSON.parse(await data.text()));
  return data;
}

export async function listSkills(
  scope: 'plaza' | 'mine',
  { limit = 50, offset = 0 }: { limit?: number; offset?: number } = {},
): Promise<Skill[]> {
  if (scope === 'mine') {
    const { data, error, response } = await apiClient.GET('/skills');
    if (!data) throw await toApiFailure(response, error);
    return data;
  }

  const { data, error, response } = await apiClient.GET('/skills/plaza', {
    params: { query: { limit, offset } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function getSkill(skillId: string): Promise<Skill> {
  const { data, error, response } = await apiClient.GET('/skills/{skill_id}', {
    params: { path: { skill_id: skillId } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function createSkill(payload: SkillCreate): Promise<Skill> {
  const { data, error, response } = await apiClient.POST('/skills', { body: payload });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function updateSkill(skillId: string, payload: SkillUpdate): Promise<Skill> {
  const { data, error, response } = await apiClient.PATCH('/skills/{skill_id}', {
    params: { path: { skill_id: skillId } },
    body: payload,
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function deleteSkill(skillId: string): Promise<void> {
  const { error, response } = await apiClient.DELETE('/skills/{skill_id}', {
    params: { path: { skill_id: skillId } },
  });
  if (!response.ok) throw await toApiFailure(response, error);
}

export async function getGenerationQuota(): Promise<QuotaInfo> {
  const { data, error, response } = await apiClient.GET('/skills/_/quota');
  if (!data) throw await toApiFailure(response, error);
  return data;
}
