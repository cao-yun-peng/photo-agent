import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type SearchResult = components['schemas']['SearchResult'];
export type SearchResultItem = components['schemas']['SearchResultItem'];

export async function searchPhotos(
  query: string,
  cursor?: string | null,
): Promise<SearchResult> {
  const { data, error, response } = await apiClient.POST('/search', {
    body: {
      q: query,
      limit: 24,
      result_mode: 'browse',
      complete_result_set: false,
      status: 'done',
      w_semantic: 0.7,
      w_recency: 0.2,
      w_interaction: 0.1,
      cursor: cursor || null,
      auto_parse: true,
      verify_constraints: true,
      verify_semantic: true,
    },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function reportSearchClick(
  photoId: string,
  query: string,
  rank: number,
): Promise<void> {
  const { error, response } = await apiClient.POST('/search/click', {
    body: { photo_id: photoId, query, rank },
  });
  if (!response.ok) throw await toApiFailure(response, error);
}
