import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type PhotoListItem = components['schemas']['PhotoListItem'];
export type PhotoDetail = components['schemas']['PhotoOut'];
export type PhotoProcessingStatus = components['schemas']['PhotoProcessingStatus'];
export type UploadUrlRequest = components['schemas']['UploadUrlRequest'];
export type UploadUrlResponse = components['schemas']['UploadUrlResponse'];

export async function listPhotos({
  limit = 24,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<PhotoListItem[]> {
  const { data, error, response } = await apiClient.GET('/photos', {
    params: { query: { limit, offset } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function getPhoto(photoId: string): Promise<PhotoDetail> {
  const { data, error, response } = await apiClient.GET('/photos/{photo_id}', {
    params: { path: { photo_id: photoId } },
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function requestUploadUrl(
  payload: UploadUrlRequest,
): Promise<UploadUrlResponse> {
  const { data, error, response } = await apiClient.POST('/photos/upload-url', {
    body: payload,
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function finishUpload(payload: {
  oss_key: string;
  hash: string;
  size_bytes: number;
  mime_type: string;
}): Promise<PhotoDetail> {
  const { data, error, response } = await apiClient.POST('/photos', {
    body: payload,
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function getProcessingStatuses(
  photoIds: string[],
): Promise<PhotoProcessingStatus[]> {
  const { data, error, response } = await apiClient.POST(
    '/photos/processing-status/batch',
    { body: { photo_ids: photoIds } },
  );
  if (!data) throw await toApiFailure(response, error);
  return data.items;
}

export async function retrySearchIndex(
  photoId: string,
): Promise<PhotoProcessingStatus> {
  const { data, error, response } = await apiClient.POST(
    '/photos/{photo_id}/retry-search-index',
    { params: { path: { photo_id: photoId } } },
  );
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function deletePhoto(photoId: string): Promise<void> {
  const { error, response } = await apiClient.DELETE('/photos/{photo_id}', {
    params: { path: { photo_id: photoId } },
  });
  if (!response.ok) throw await toApiFailure(response, error);
}

export async function reportPhotoView(photoId: string, context: string): Promise<void> {
  const { error, response } = await apiClient.POST('/photos/{photo_id}/interact', {
    params: { path: { photo_id: photoId } },
    body: { action: 'view', context },
  });
  if (!response.ok) throw await toApiFailure(response, error);
}
