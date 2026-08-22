import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type LoginRequest = components['schemas']['LoginRequest'];
export type TokenResponse = components['schemas']['TokenResponse'];
export type CurrentUser = components['schemas']['UserOut'];

export async function loginWithDevelopmentUser(
  payload: LoginRequest,
): Promise<TokenResponse> {
  const { data, error, response } = await apiClient.POST('/auth/wechat', {
    body: payload,
  });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function getCurrentUser(): Promise<CurrentUser> {
  const { data, error, response } = await apiClient.GET('/auth/me');
  if (!data) throw await toApiFailure(response, error);
  return data;
}
