import type { components } from './generated';
import { apiClient, toApiFailure } from './client';

export type LoginRequest = components['schemas']['LoginRequest'];
export type TokenResponse = components['schemas']['TokenResponse'];
export type CurrentUser = components['schemas']['UserOut'];
export type WebLoginRequest = components['schemas']['WebLoginRequest'];

export async function getAuthOptions() {
  const { data, error, response } = await apiClient.GET('/auth/options');
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function loginWithPassword(payload: WebLoginRequest): Promise<TokenResponse> {
  const { data, error, response } = await apiClient.POST('/auth/login', { body: payload });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

export async function registerWithPassword(payload: WebLoginRequest): Promise<TokenResponse> {
  const { data, error, response } = await apiClient.POST('/auth/register', { body: payload });
  if (!data) throw await toApiFailure(response, error);
  return data;
}

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
