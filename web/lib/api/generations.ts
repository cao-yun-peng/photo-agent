import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type Generation = components['schemas']['GenerationOut'];
export type GenerateRequest = components['schemas']['GenerateRequest'];

export const ACTIVE_GENERATION_STATUSES = new Set(['pending', 'processing']);
export const TERMINAL_GENERATION_STATUSES = new Set(['done', 'failed']);

export function shouldPollGeneration(status?: string | null): boolean {
  return Boolean(status && ACTIVE_GENERATION_STATUSES.has(status));
}

export function requiresGenerationConfirmation(status?: string | null): boolean {
  return status === 'awaiting_confirmation' || status === 'queue_failed';
}

export function createIdempotencyKey(): string {
  const random = crypto.randomUUID?.() || Math.random().toString(36).slice(2);
  return `web-gen-${Date.now().toString(36)}-${random}`;
}

export async function prepareGeneration(
  photoId: string,
  payload: GenerateRequest,
): Promise<Generation> {
  const { data, error, response } = await apiClient.POST('/photos/{photo_id}/generate', {
    params: { path: { photo_id: photoId } },
    body: payload,
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function confirmGeneration(
  generationId: string,
  confirmationToken: string,
): Promise<Generation> {
  const { data, error, response } = await apiClient.POST(
    '/generations/{generation_id}/confirm',
    {
      params: { path: { generation_id: generationId } },
      body: { confirmation_token: confirmationToken },
    },
  );
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function listGenerations({
  limit = 50,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<Generation[]> {
  const { data, error, response } = await apiClient.GET('/generations', {
    params: { query: { limit, offset } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function getGeneration(generationId: string): Promise<Generation> {
  const { data, error, response } = await apiClient.GET('/generations/{generation_id}', {
    params: { path: { generation_id: generationId } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}
