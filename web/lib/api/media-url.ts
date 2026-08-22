import { API_ORIGIN } from './client';

export function resolveMediaUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value, `${API_ORIGIN}/`).toString();
  } catch {
    return null;
  }
}
